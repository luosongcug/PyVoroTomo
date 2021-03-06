import mpi4py.MPI as MPI
import numpy as np
import os
import pandas as pd
import pykonal
import scipy.sparse
import scipy.spatial

import _dataio
import _constants
import _utilities

# Get logger handle.
logger = _utilities.get_logger(f"__main__.{__name__}")

# Define aliases.
PointSourceSolver = pykonal.solver.PointSourceSolver
geo2sph = pykonal.transformations.geo2sph
sph2geo = pykonal.transformations.sph2geo
sph2xyz = pykonal.transformations.sph2xyz

COMM       = MPI.COMM_WORLD
RANK       = COMM.Get_rank()
WORLD_SIZE = COMM.Get_size()
ROOT_RANK  = _constants.ROOT_RANK


class InversionIterator(object):
    """
    A class providing core functionality for iterating inversion
    procedure.
    """

    def __init__(self, argc):
        self._argc = argc
        self._arrivals = None
        self._cfg = None
        self._events = None
        self._iiter = 0
        self._projection_matrix = None
        self._pwave_model = None
        self._swave_model = None
        self._pwave_realization_stack = None
        self._swave_realization_stack = None
        self._pwave_variance = None
        self._swave_variance = None
        self._residuals = None
        self._sensitivity_matrix = None
        self._stations = None
        self._sampled_arrivals = None
        self._voronoi_cells = None

    @property
    def argc(self):
        return (self._argc)

    @property
    def arrivals(self):
        return (self._arrivals)

    @arrivals.setter
    def arrivals(self, value):
        self._arrivals = value

    @property
    def cfg(self):
        return (self._cfg)

    @cfg.setter
    def cfg(self, value):
        self._cfg = value

    @property
    def events(self):
        return (self._events)

    @events.setter
    def events(self, value):
        self._events = value

    @property
    def iiter(self):
        return (self._iiter)

    @iiter.setter
    def iiter(self, value):
        self._iiter = value

    @property
    def projection_matrix(self):
        return (self._projection_matrix)

    @projection_matrix.setter
    def projection_matrix(self, value):
        self._projection_matrix = value

    @property
    def pwave_model(self):
        return (self._pwave_model)

    @pwave_model.setter
    def pwave_model(self, value):
        self._pwave_model = value

    @property
    def pwave_realization_stack(self):
        if self._pwave_realization_stack is None:
           self._pwave_realization_stack = []
        return (self._pwave_realization_stack)

    @pwave_realization_stack.setter
    def pwave_realization_stack(self, value):
        self._pwave_realization_stack = value

    @property
    def pwave_variance(self):
        stack = np.stack(self.pwave_realization_stack)
        var = np.var(stack, axis=0)
        return (var)

    @property
    def residuals(self):
        return (self._residuals)

    @residuals.setter
    def residuals(self, value):
        self._residuals = value

    @property
    def sampled_arrivals(self):
        return (self._sampled_arrivals)

    @sampled_arrivals.setter
    def sampled_arrivals(self, value):
        self._sampled_arrivals = value

    @property
    def sensitivity_matrix(self):
        return (self._sensitivity_matrix)

    @sensitivity_matrix.setter
    def sensitivity_matrix(self, value):
        self._sensitivity_matrix = value

    @property
    def stations(self):
        return (self._stations)

    @stations.setter
    def stations(self, value):
        self._stations = value

    @property
    def swave_model(self):
        return (self._swave_model)

    @swave_model.setter
    def swave_model(self, value):
        self._swave_model = value

    @property
    def swave_realization_stack(self):
        if self._swave_realization_stack is None:
           self._swave_realization_stack = []
        return (self._swave_realization_stack)

    @swave_realization_stack.setter
    def swave_realization_stack(self, value):
        self._swave_realization_stack = value

    @property
    def swave_variance(self):
        stack = np.stack(self.swave_realization_stack)
        var = np.var(stack, axis=0)
        return (var)

    @property
    def voronoi_cells(self):
        return (self._voronoi_cells)

    @voronoi_cells.setter
    def voronoi_cells(self, value):
        self._voronoi_cells = value


    @_utilities.log_errors(logger)
    @_utilities.root_only(RANK)
    def _compute_model_update(self, phase):
        """
        Compute the model update for a single realization and appends
        the results to the realization stack.

        Only the root rank performs this operation.
        """

        logger.info(f"Computing {phase}-wave model update")

        if phase == "P":
            model = self.pwave_model
        elif phase == "S":
            model = self.swave_model
        else:
            raise (ValueError(f"Unrecognized phase ({phase}) supplied."))

        damp = self.cfg["algorithm"]["damp"]
        atol = self.cfg["algorithm"]["atol"]
        btol = self.cfg["algorithm"]["btol"]
        conlim = self.cfg["algorithm"]["conlim"]
        maxiter = self.cfg["algorithm"]["maxiter"]

        result = scipy.sparse.linalg.lsmr(
            self.sensitivity_matrix,
            self.residuals,
            damp,
            atol,
            btol,
            conlim,
            maxiter,
            show=False
        )
        x, istop, itn, normr, normar, norma, conda, normx = result
        delta_slowness = self.projection_matrix * x
        delta_slowness = delta_slowness.reshape(model.npts)
        slowness = np.power(model.values, -1) + delta_slowness
        velocity = np.power(slowness, -1)

        if phase == "P":
            self.pwave_realization_stack.append(velocity)
        else:
            self.swave_realization_stack.append(velocity)

        return (True)


    @_utilities.log_errors(logger)
    def _compute_sensitivity_matrix(self, phase):
        """
        Compute the sensitivity matrix.
        """

        logger.info(f"Computing {phase}-wave sensitivity matrix")

        nvoronoi = self.cfg["algorithm"]["nvoronoi"]
        traveltime_dir = self.cfg["workspace"]["traveltime_dir"]

        index_keys = ["network", "station"]
        arrivals = self.sampled_arrivals.set_index(index_keys)

        arrivals = arrivals.sort_index()

        if RANK == ROOT_RANK:
            ids = arrivals.index.unique()
            self._dispatch(ids)

            logger.debug("Compiling sensitivity matrix.")
            column_idxs = COMM.gather(None, root=ROOT_RANK)
            nsegments = COMM.gather(None, root=ROOT_RANK)
            nonzero_values = COMM.gather(None, root=ROOT_RANK)
            residuals = COMM.gather(None, root=ROOT_RANK)

            column_idxs = list(filter(lambda x: x is not None, column_idxs))
            nsegments = list(filter(lambda x: x is not None, nsegments))
            nonzero_values = list(filter(lambda x: x is not None, nonzero_values))
            residuals = list(filter(lambda x: x is not None, residuals))


            column_idxs = np.concatenate(column_idxs)
            nonzero_values = np.concatenate(nonzero_values)
            residuals = np.concatenate(residuals)
            nsegments = np.concatenate(nsegments)

            row_idxs = [
                i for i in range(len(nsegments))
                  for j in range(nsegments[i])
            ]
            row_idxs = np.array(row_idxs)

            matrix = scipy.sparse.coo_matrix(
                (nonzero_values, (row_idxs, column_idxs)),
                shape=(len(nsegments), nvoronoi)
            )

            self.sensitivity_matrix = matrix
            self.residuals = residuals

        else:

            column_idxs = np.array([], dtype=_constants.DTYPE_INT)
            nsegments = np.array([], dtype=_constants.DTYPE_INT)
            nonzero_values = np.array([], dtype=_constants.DTYPE_REAL)
            residuals = np.array([], dtype=_constants.DTYPE_REAL)

            events = self.events.set_index("event_id")
            events = events.sort_index()

            while True:

                item = self._request_dispatch()

                if item is None:
                    logger.debug("Sentinel received. Gathering sensitivity matrix.")

                    column_idxs = COMM.gather(column_idxs, root=ROOT_RANK)
                    nsegments = COMM.gather(nsegments, root=ROOT_RANK)
                    nonzero_values = COMM.gather(nonzero_values, root=ROOT_RANK)
                    residuals = COMM.gather(residuals, root=ROOT_RANK)

                    break

                network, station = item

                # Get the subset of arrivals belonging to this station.
                _arrivals = arrivals.loc[(network, station)]
                _arrivals = _arrivals.set_index("event_id")

                # Initialize the ray tracer.
                path = os.path.join(traveltime_dir, f"{network}.{station}.{phase}.npz")
                traveltime = pykonal.fields.load(path)

                # Load velocity model for calculating dt/dx
                vel = pykonal.fields.load(vel)

                step_size = traveltime.step_size

                for event_id, arrival in _arrivals.iterrows():
                    event = events.loc[event_id]
                    event_coords = event[["latitude", "longitude", "depth"]]
                    event_coords = geo2sph(event_coords)
                    raypath = traveltime.trace_ray(event_coords)
                    if vel_invert: # For velocity inversion
                        _column_idxs, counts = self._projected_ray_idxs(raypath)
                        nonzero_values = np.append(nonzero_values, counts * step_size)
                        column_idxs = np.append(column_idxs, _column_idxs)
                        nsegments = np.append(nsegments, len(_column_idxs))
                    else: # For hypocenter inversion
                        _column_idxs, _nonnzero_values = self._calculate_hypo_sensitivity(event_id,event_coords,raypath,vel)
                        column_idxs = np.append(column_idxs, _column_idxs)
                        nonzero_values = np.append(nonzero_values,_nonnzero_values)
                        nsegments = np.append(nsegments, len(_column_idxs))
                    residuals = np.append(residuals, arrival["residual"])

        COMM.barrier()

        return (True)


    @_utilities.log_errors(logger)
    def _dispatch(self, ids, sentinel=None):
        """
        Dispatch ids to hungry workers, then dispatch sentinels.
        """

        logger.debug("Dispatching ids")

        for _id in ids:
            requesting_rank = COMM.recv(
                source=MPI.ANY_SOURCE,
                tag=_constants.DISPATCH_REQUEST_TAG
            )
            COMM.send(
                _id,
                dest=requesting_rank,
                tag=_constants.DISPATCH_TRANSMISSION_TAG
            )
        # Distribute sentinel.
        for irank in range(WORLD_SIZE - 1):
            requesting_rank = COMM.recv(
                source=MPI.ANY_SOURCE,
                tag=_constants.DISPATCH_REQUEST_TAG
            )
            COMM.send(
                sentinel,
                dest=requesting_rank,
                tag=_constants.DISPATCH_TRANSMISSION_TAG
            )

        return (True)


    @_utilities.log_errors(logger)
    def _generate_voronoi_cells(self, adaptive=False, phase=None):
        """
        Generate Voronoi cells.
        """

        if adaptive is False:
            self._generate_voronoi_cells_random()
        else:
            self._generate_voronoi_cells_adaptive(phase)


    @_utilities.log_errors(logger)
    def _generate_voronoi_cells_random(self):
        """
        Generate randomly distributed Voronoi cells.
        """

        if RANK == ROOT_RANK:
            min_coords = self.pwave_model.min_coords
            max_coords = self.pwave_model.max_coords
            delta = (max_coords - min_coords)
            nvoronoi = self.cfg["algorithm"]["nvoronoi"]
            cells = np.random.rand(nvoronoi, 3) * delta + min_coords
            self.voronoi_cells = cells

        self.synchronize(attrs=["voronoi_cells"])

        return (True)


    @_utilities.log_errors(logger)
    def _generate_voronoi_cells_adaptive(self, phase):
        """
        Generate Voronoi cells adaptively.
        """

        if RANK == ROOT_RANK:

            nvoronoi = self.cfg["algorithm"]["nvoronoi"]
            arrivals = self.sampled_arrivals.sample(n=nvoronoi)
            arrivals = arrivals.set_index(["network", "station"])
            arrivals = arrivals.sort_index()
            index = arrivals.index.unique()
            event_ids = [tuple(arrivals.loc[idx, "event_id"].values) for idx in index]
            items = zip(index, event_ids)
            self._dispatch(items)

            voronoi_cells = COMM.gather(None, root=ROOT_RANK)
            voronoi_cells = filter(lambda item: item is not None, voronoi_cells)
            voronoi_cells = sum(voronoi_cells, [])
            voronoi_cells = np.stack(voronoi_cells)
            self.voronoi_cells = voronoi_cells

        else:

            traveltime_dir = self.cfg["workspace"]["traveltime_dir"]
            events = self.events
            events = events.set_index("event_id")

            voronoi_cells = []

            while True:

                item = self._request_dispatch()

                if item is None:
                    COMM.gather(voronoi_cells, root=ROOT_RANK)
                    break

                (network, station), event_ids = item

                filename = f"{network}.{station}.{phase}.npz"
                path = os.path.join(traveltime_dir, filename)
                traveltime = pykonal.fields.load(path)

                for event_id in event_ids:
                    keys = ["latitude", "longitude", "depth"]
                    coords = events.loc[event_id, keys]
                    coords = geo2sph(coords)
                    raypath = traveltime.trace_ray(coords)
                    idx = np.random.choice(range(len(raypath)))
                    coords = raypath[idx]
                    voronoi_cells.append(coords)

        self.synchronize(attrs=["voronoi_cells"])

        return (True)


    @_utilities.log_errors(logger)
    def _projected_ray_idxs(self, raypath):
        """
        Return the cell IDs (column IDs) of each segment of the given
        raypath and the length of each segment in counts.
        """

        voronoi_cells = sph2xyz(self.voronoi_cells, (0, 0, 0))
        tree = scipy.spatial.cKDTree(voronoi_cells)
        raypath = sph2xyz(raypath, (0, 0, 0))
        _, column_idxs = tree.query(raypath)
        column_idxs, counts = np.unique(column_idxs, return_counts=True)

        return (column_idxs, counts)

    def _calculate_hypo_sensitivity(event_id,event_coords,raypath,vel):
        '''
        Retrun the column index and direvitive of data residual w.r.t hypocenter
        '''
        #raypath, 3D array (rad,lon,lat) with lat and lon in rad
        dpos = np.zeros(4,)
        # raypath from station to event
        dpos[0] = raypath[-1,0]-raypath[-2,0]
        dpos[1] = raypath[-1,0]*(raypath[-1,1]-raypath[-2,1])
        dpos[2] = raypath[-1,0]*(raypath[-1,2]-raypath[-2,2])*np.cos(raypath[-1,1])

        # this is a unit vector point to the direction of the ray path (first two points in a ray)
        dpos = dpos/np.sqrt(np.sum(dpos**2))
        # interpolate velocity model to the hypocenter point (rad0,lon0,lat0)
        # using pykonal here? vel 
        vel_hypo = interpolate(vel,event_coords)
        dtdx = np.zeros(4,)
        # the derivative is just the projection of slowness to different direction
        dtdx[:-1] = dpos/vel_hypo
        dtdx[-1] = -1.0
        column_id = np.arange(event_id*4,event_id*4+4)
        return column_id,dtdx

    @_utilities.log_errors(logger)
    def _request_dispatch(self):
        """
        Request, receive, and return item from dispatcher.
        """
        COMM.send(
            RANK,
            dest=ROOT_RANK,
            tag=_constants.DISPATCH_REQUEST_TAG
        )
        item = COMM.recv(
            source=ROOT_RANK,
            tag=_constants.DISPATCH_TRANSMISSION_TAG
        )

        return (item)

    @_utilities.log_errors(logger)
    def _sample_arrivals(self, phase):
        """
        Draw a random sample of arrivals and update the
        "sampled_arrivals" attribute.
        """

        if RANK == ROOT_RANK:
            narrival = self.cfg["algorithm"]["narrival"]
            tukey_k = self.cfg["algorithm"]["outlier_removal_factor"]

            # Subset for the appropriate phase.
            arrivals = self.arrivals.set_index("phase")
            arrivals = arrivals.sort_index()
            arrivals = arrivals.loc[phase]

            # Remove outliers.
            q1, q3 = arrivals["residual"].quantile(q=[0.25, 0.75])
            iqr = q3 - q1
            min_residual = q1 - tukey_k * iqr
            max_residual = q3 + tukey_k * iqr
            arrivals = arrivals[
                 (arrivals["residual"] > min_residual)
                &(arrivals["residual"] < max_residual)
            ]

            self.sampled_arrivals = arrivals.sample(n=narrival)

        self.synchronize(attrs=["sampled_arrivals"])

        return (True)


    @_utilities.log_errors(logger)
    def _update_projection_matrix(self):
        """
        Update the projection matrix using the current Voronoi cells.
        """

        logger.info("Updating projection matrix")

        nvoronoi = self.cfg["algorithm"]["nvoronoi"]

        if RANK == ROOT_RANK:
            voronoi_cells = sph2xyz(self.voronoi_cells, origin=(0,0,0))
            tree = scipy.spatial.cKDTree(voronoi_cells)
            nodes = self.pwave_model.nodes.reshape(-1, 3)
            nodes = sph2xyz(nodes, origin=(0,0,0))
            _, column_ids = tree.query(nodes)

            nnodes = np.prod(self.pwave_model.nodes.shape[:-1])
            row_ids = np.arange(nnodes)

            values = np.ones(nnodes,)
            self.projection_matrix = scipy.sparse.coo_matrix(
                (values, (row_ids, column_ids)),
                shape=(nnodes, nvoronoi)
            )

        self.synchronize(attrs=["projection_matrix"])

        return (True)


    @_utilities.log_errors(logger)
    def compute_traveltime_lookup_tables(self):
        """
        Compute traveltime-lookup tables.
        """

        logger.info("Computing traveltime-lookup tables.")

        traveltime_dir = self.cfg["workspace"]["traveltime_dir"]

        if RANK == ROOT_RANK:

            os.makedirs(traveltime_dir, exist_ok=True)
            ids = zip(self.stations["network"], self.stations["station"])
            self._dispatch(sorted(ids))

        else:

            geometry = self.stations
            geometry = geometry.set_index(["network", "station"])

            while True:

                # Request an event
                item = self._request_dispatch()

                if item is None:
                    logger.debug("Received sentinel.")

                    break

                network, station = item

                keys = ["latitude", "longitude", "depth"]
                coords = geometry.loc[(network, station), keys]
                coords = geo2sph(coords)

                for phase, model in (("P", self.pwave_model), ("S", self.swave_model)):
                    solver = PointSourceSolver(coord_sys="spherical")
                    solver.vv.min_coords = model.min_coords
                    solver.vv.node_intervals = model.node_intervals
                    solver.vv.npts = model.npts
                    solver.vv.values = model.values
                    solver.src_loc = coords
                    solver.solve()
                    path = os.path.join(
                        traveltime_dir,
                        f"{network}.{station}.{phase}.npz"
                    )
                    solver.tt.savez(path)

        COMM.barrier()

        return (True)


    @_utilities.log_errors(logger)
    def iterate(self):
        """
        Execute one iteration the entire inversion procedure including
        updating velocity models, event locations, and arrival residuals.
        """

        niter = self.cfg["algorithm"]["niter"]
        nreal = self.cfg["algorithm"]["nreal"]
        output_dir = self.cfg["workspace"]["output_dir"]
        adaptive_voronoi = self.cfg["algorithm"]["adaptive_voronoi_cells"]

        self.iiter += 1

        logger.info(f"Iteration #{self.iiter} (/{niter}).")

        for phase in ("P", "S"):
            logger.info(f"Updating {phase}-wave model")
            for ireal in range(nreal):
                logger.info(f"Realization #{ireal+1} (/{nreal})")
                self._sample_arrivals(phase)
                self._generate_voronoi_cells(
                    adaptive=adaptive_voronoi,
                    phase=phase
                )
                self._update_projection_matrix()
                self._compute_sensitivity_matrix(phase)
                self._compute_model_update(phase)
        self.update_models()
        self.compute_traveltime_lookup_tables()
        self.relocate_events()
        self.update_arrival_residuals()
        self.save(output_dir)


    @_utilities.log_errors(logger)
    def load_cfg(self):
        """
        Parse and store configuration-file parameters.

        ROOT_RANK parses configuration file and broadcasts contents to all
        other processes.
        """

        logger.info("Loading configuration-file parameters.")

        if RANK == ROOT_RANK:

            # Parse configuration-file parameters.
            self.cfg = _utilities.parse_cfg(self.argc.configuration_file)

        self.synchronize(attrs=["cfg"])

        return (True)


    @_utilities.log_errors(logger)
    def load_event_data(self):
        """
        Parse and return event data from file.

        ROOT_RANK parses file and broadcasts contents to all other
        processes.
        """

        logger.info("Loading event data.")

        if RANK == ROOT_RANK:

            # Parse event data.
            data = _dataio.parse_event_data(self.argc)
            self.events, self.arrivals = data

        self.synchronize(attrs=["events"])

        return (True)


    @_utilities.log_errors(logger)
    def load_network_geometry(self):
        """
        Parse and return network geometry from file.

        ROOT_RANK parses file and broadcasts contents to all other
        processes.
        """

        logger.info("Loading network geometry")

        if RANK == ROOT_RANK:

            # Parse event data.
            stations = _dataio.parse_network_geometry(self.argc)
            self.stations = stations

        self.synchronize(attrs=["stations"])

        return (True)


    @_utilities.log_errors(logger)
    def load_velocity_models(self):
        """
        Parse and return velocity models from file.

        ROOT_RANK parses file and broadcasts contents to all other
        processes.
        """

        logger.info("Loading velocity models.")

        if RANK == ROOT_RANK:

            # Parse velocity model files.
            velocity_models = _dataio.parse_velocity_models(self.cfg)
            self.pwave_model, self.swave_model = velocity_models

        self.synchronize(attrs=["pwave_model", "swave_model"])

        return (True)


    @_utilities.log_errors(logger)
    def relocate_events(self):
        """
        Relocate all events and update the "events" attribute.
        """

        logger.info("Relocating events.")

        traveltime_dir = self.cfg["workspace"]["traveltime_dir"]
        if RANK == ROOT_RANK:
            ids = self.events["event_id"]
            self._dispatch(sorted(ids))

            logger.debug("Dispatch complete. Gathering events.")
            # Gather and concatenate events from all workers.
            events = COMM.gather(None, root=ROOT_RANK)
            events = pd.concat(events, ignore_index=True)
            events = events.convert_dtypes()
            self.events = events

        else:
            # Define columns to output.
            columns = [
                "latitude",
                "longitude",
                "depth",
                "time",
                "residual",
                "event_id"
            ]


            # Initialize EQLocator object.
            locator = pykonal.locate.EQLocator(
                station_dict(self.stations),
                tt_dir=traveltime_dir
            )
            locator.grid.min_coords     = self.pwave_model.min_coords
            locator.grid.node_intervals = self.pwave_model.node_intervals
            locator.grid.npts           = self.pwave_model.npts
            locator.pwave_velocity      = self.pwave_model.values
            locator.swave_velocity      = self.swave_model.values

            # Create some aliases for configuration-file parameters.
            dlat = self.cfg["locate"]["dlat"]
            dlon = self.cfg["locate"]["dlon"]
            dz = self.cfg["locate"]["ddepth"]
            dt = self.cfg["locate"]["dtime"]

            events = pd.DataFrame()

            while True:

                # Request an event
                event_id = self._request_dispatch()

                if event_id is None:
                    logger.debug("Received sentinel, gathering events.")
                    COMM.gather(events, root=ROOT_RANK)

                    break

                logger.debug(f"Received event ID #{event_id}")

                # Clear arrivals from previous event.
                locator.clear_arrivals()
                locator.add_arrivals(arrival_dict(self.arrivals, event_id))
                locator.load_traveltimes()
                loc = locator.locate(dlat=dlat, dlon=dlon, dz=dz, dt=dt)

                # Get residual RMS, reformat result, and append to events
                # DataFrame.
                rms = locator.rms(loc)
                loc[:3] = sph2geo(loc[:3])
                event = pd.DataFrame(
                    [np.concatenate((loc, [rms, event_id]))],
                    columns=columns
                )
                events = events.append(event, ignore_index=True)

        self.synchronize(attrs=["events"])

        return (True)



    @_utilities.log_errors(logger)
    def sanitize_data(self):
        """
        Sanitize input data.
        """

        logger.info("Sanitizing data.")

        if RANK == ROOT_RANK:

            # Drop duplicate stations.
            self.stations = self.stations.drop_duplicates(["network", "station"])

            # Drop stations without arrivals.
            logger.debug("Dropping stations without arrivals.")
            arrivals = self.arrivals.set_index(["network", "station"])
            idx_keep = arrivals.index.unique()
            stations = self.stations.set_index(["network", "station"])
            stations = stations.loc[idx_keep]
            stations = stations.reset_index()
            self.stations = stations

        self.synchronize(attrs=["stations"])

        return (True)


    @_utilities.log_errors(logger)
    @_utilities.root_only(RANK)
    def save(self, output_dir):
        """
        Save the current "events", "arrivals", "pwave_model",
        "pwave_realization_stack", "pwave_variance", "swave_model",
        "swave_realization_stack", and "swave_variance" to disk.

        "events" and "arrivals" are written to a HDF5 file
        using pandas.HDFStore and the remaining attributes
        are written to a NPZ file with handles "pwave_model",
        "swave_model", "pwave_stack", "swave_stack".
        """

        logger.info(f"Saving data from iteration #{self.iiter}")

        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"{self.iiter:02d}")

        if self.iiter > 0:
            self.pwave_model.savez(path + ".pwave_model")
            self.swave_model.savez(path + ".swave_model")
            pwave_stack = np.stack(self.pwave_realization_stack)
            swave_stack = np.stack(self.pwave_realization_stack)
            np.savez(
                f"{path}.realizations.npz",
                pwave_stack=pwave_stack,
                pwave_variance=self.pwave_variance,
                swave_stack=swave_stack,
                swave_variance=self.swave_variance,
                min_coords=self.pwave_model.min_coords,
                node_intervals=self.pwave_model.node_intervals,
                npts=self.pwave_model.npts
            )

        events       = self.events
        EVENT_DTYPES = _constants.EVENT_DTYPES
        for column in EVENT_DTYPES:
            events[column] = events[column].astype(EVENT_DTYPES[column])

        arrivals       = self.arrivals
        ARRIVAL_DTYPES = _constants.ARRIVAL_DTYPES
        for column in ARRIVAL_DTYPES:
            arrivals[column] = arrivals[column].astype(ARRIVAL_DTYPES[column])

        events.to_hdf(f"{path}.events.h5", key="events")
        arrivals.to_hdf(f"{path}.events.h5", key="arrivals")

        return(True)



    @_utilities.log_errors(logger)
    def synchronize(self, attrs="all"):
        """
        Synchronize input data across all processes.

        "attrs" may be an iterable of attribute names to synchronize.
        """


        _all = (
            "arrivals",
            "cfg",
            "events",
            "projection_matrix",
            "pwave_model",
            "swave_model",
            "sampled_arrivals",
            "stations",
            "voronoi_cells"
        )

        if attrs == "all":
            attrs = _all

        for attr in attrs:
            value = getattr(self, attr) if RANK == ROOT_RANK else None
            value = COMM.bcast(value, root=ROOT_RANK)
            setattr(self, attr, value)

        COMM.barrier()

        return (True)


    @_utilities.log_errors(logger)
    def update_arrival_residuals(self):
        """
        Compute arrival-time residuals based on current event locations
        and velocity models, and update "residual" columns of "arrivals"
        attribute.
        """

        logger.info("Updating arrival residuals.")

        traveltime_dir = self.cfg["workspace"]["traveltime_dir"]
        arrivals = self.arrivals.set_index(["network", "station", "phase"])
        arrivals = arrivals.sort_index()

        if RANK == ROOT_RANK:
            ids = arrivals.index.unique()
            self._dispatch(ids)
            logger.debug("Dispatch complete. Gathering arrivals.")
            arrivals = COMM.gather(None, root=ROOT_RANK)
            arrivals = pd.concat(arrivals, ignore_index=True)
            arrivals = arrivals.convert_dtypes()
            self.arrivals = arrivals

        else:

            events = self.events.set_index("event_id")
            updated_arrivals = pd.DataFrame()

            while True:

                # Request an event
                item = self._request_dispatch()

                if item is None:
                    logger.debug("Received sentinel. Gathering arrivals.")
                    COMM.gather(updated_arrivals, root=ROOT_RANK)

                    break


                network, station, phase = item
                logger.debug(f"Updating {phase}-wave residuals for {network}.{station}.")

                path = os.path.join(traveltime_dir, f"{network}.{station}.{phase}.npz")
                traveltime = pykonal.fields.load(path)

                _arrivals = arrivals.loc[(network, station, phase)]
                _arrivals = _arrivals.set_index("event_id")

                for event_id, arrival in _arrivals.iterrows():
                    arrival_time = arrival["time"]
                    origin_time = events.loc[event_id, "time"]
                    coords = events.loc[event_id, ["latitude", "longitude", "depth"]]
                    coords = geo2sph(coords)
                    residual = arrival_time - (origin_time + traveltime.value(coords))
                    arrival = dict(
                        network=network,
                        station=station,
                        phase=phase,
                        event_id=event_id,
                        time=arrival_time,
                        residual=residual
                    )
                    arrival = pd.DataFrame([arrival])
                    updated_arrivals = updated_arrivals.append(arrival, ignore_index=True)

        self.synchronize(attrs=["arrivals"])

        return (True)

    @_utilities.log_errors(logger)
    def update_models(self):
        """
        Stack random realizations to obtain average model and update
        appropriate attributes.
        """

        if RANK == ROOT_RANK:
            stack = np.stack(self.pwave_realization_stack)
            self.pwave_model.values = np.mean(stack, axis=0)

            stack = np.stack(self.swave_realization_stack)
            self.swave_model.values = np.mean(stack, axis=0)

        self.synchronize(attrs=["pwave_model", "swave_model"])

        return (True)



@_utilities.log_errors(logger)
def arrival_dict(dataframe, event_id):
    """
    Return a dictionary with phase-arrival data suitable for passing to
    the EQLocator.add_arrivals() method.

    Returned dictionary has ("station_id", "phase") keys, where
    "station_id" = f"{network}.{station}", and values are
    phase-arrival timestamps.
    """

    dataframe = dataframe.set_index("event_id")
    fields = ["network", "station", "phase", "time"]
    dataframe = dataframe.loc[event_id, fields]

    _arrival_dict = {
        (f"{network}.{station}", phase): timestamp
        for network, station, phase, timestamp in dataframe.values
    }

    return (_arrival_dict)


@_utilities.log_errors(logger)
def station_dict(dataframe):
    """
    Return a dictionary with network geometry suitable for passing to
    the EQLocator constructor.

    Returned dictionary has "station_id" keys, where "station_id" =
    f"{network}.{station}", and values are spherical coordinates of
    station locations.
    """

    if np.any(dataframe[["network", "station"]].duplicated()):
        raise (IOError("Multiple coordinates supplied for single station(s)"))

    dataframe = dataframe.set_index(["network", "station"])

    _station_dict = {
        f"{network}.{station}": geo2sph(
            dataframe.loc[
                (network, station),
                ["latitude", "longitude", "depth"]
            ].values
        ) for network, station in dataframe.index
    }

    return (_station_dict)
