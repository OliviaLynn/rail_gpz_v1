"""
RAIL wrapping of Peter Hatfield's version of GPz, which
can be found at:
https://github.com/pwhatfield/GPz_py3
"""
import numpy as np
from ceci.config import StageParameter as Param
from rail.core.common_params import SHARED_PARAMS
from rail.estimation.estimator import CatEstimator, CatInformer
from .GPz import GP, getOmega
import qp



def _prepare_data(data_dict, bands, err_bands, nondet_val, maglims, logflag):
    """Put data in 2D np array expected by GPz.
    For some reason they like to take the log of the magnitude errors, so
    have that as a boolean option.  Also replace nondetect vals for each
    band
    """
    numbands = len(bands)
    totrows = len(data_dict[bands[0]])
    data = np.empty([totrows, 2 * numbands])
    for i, (band, eband, lim) in enumerate(zip(bands, err_bands, maglims.values())):
        data[:, i] = data_dict[band]
        mask = np.bitwise_or(np.isclose(data_dict[band], nondet_val), np.isnan(data_dict[band]))
        data[:, i][mask] = lim
        if logflag:
            data[:, numbands + i] = np.log(data_dict[eband])
        else:  # pragma: no cover
            data[:, numbands + i] = data_dict[eband]
        data[:, numbands + i][mask] = 1.0
    return data


class Inform_GPz_v1(CatInformer):
    """Inform stage for GPz_v1
    Parameters
    ----------

    Returns
    -------
    gpz_model: model
      model file containing the trained GPz model to be used in estimate
      stage
    """
    name = "Inform_GPz_v1"
    config_options = CatInformer.config_options.copy()
    config_options.update(nondetect_val=SHARED_PARAMS,
                          mag_limits=SHARED_PARAMS,
                          trainfrac=Param(float, 0.75,
                                          msg="fraction of training data used to make tree, rest used to set best sigma"),
                          seed=Param(int, 87, msg="random seed"),
                          bands=SHARED_PARAMS,
                          err_bands=SHARED_PARAMS,
                          redshift_col=SHARED_PARAMS,
                          gpz_method=Param(str, "VC", msg="method to be used in GPz, options are 'GL', 'VL', 'GD', 'VD', 'GC', and 'VC'"),
                          n_basis=Param(int, 50, msg="number of basis functions used"),
                          learn_jointly=Param(bool, True, msg="if True, jointly learns prior linear mean function"),
                          hetero_noise=Param(bool, True, msg="if True, learns heteroscedastic noise process, set False for point est."),
                          csl_method=Param(str, "normal", msg="cost sensitive learning type, 'balanced', 'normalized', or 'normal'"),
                          csl_binwidth=Param(float, 0.1, msg="width of bin for 'balanced' cost sensitive learning"),
                          pca_decorrelate=Param(bool, True, msg="if True, decorrelate data using PCA as preprocessing stage"),
                          max_iter=Param(int, 200, msg="max number of iterations"),
                          max_attempt=Param(int, 100, msg="max iterations if no progress on validation"),
                          log_errors=Param(bool, True, msg="if true, take log of magnitude errors")
                          )

    def __init__(self, args, comm=None):
        """ Constructor
        Do CatInformer specific initialization"""
        CatInformer.__init__(self, args, comm=comm)
        self.zgrid = None

    def run(self):
        """
        train the GPz model after splitting train data into train/validation
        """
        if self.config.hdf5_groupname:
            training_data = self.get_data('input')[self.config.hdf5_groupname]
        else:  # pragma: no cover
            training_data = self.get_data('input')

        input_array = _prepare_data(training_data, self.config.bands, self.config.err_bands,
                                    self.config.nondetect_val, self.config.mag_limits,
                                    self.config.log_errors)

        sz = np.expand_dims(training_data[self.config.redshift_col], -1)
        # need permutation mask to define training vs validation
        ngal = input_array.shape[0]
        print(f"ngal: {ngal}")
        ntrain = int(ngal * self.config.trainfrac)
        randvec = np.random.permutation(ngal)
        train_mask = np.zeros(ngal, dtype=bool)
        val_mask = np.zeros(ngal, dtype=bool)
        train_mask[randvec[:ntrain]] = True
        val_mask[randvec[ntrain:]] = True

        # get weights for cost sensitive learning
        omega_weights = getOmega(sz, method=self.config.csl_method)

        # initialize model
        model = GP(self.config.n_basis,
                   method=self.config.gpz_method,
                   joint=self.config.learn_jointly,
                   heteroscedastic=self.config.hetero_noise,
                   decorrelate=self.config.pca_decorrelate,
                   seed=self.config.seed)

        print("training model...")
        model.train(input_array, sz, omega=omega_weights, training=train_mask,
                    validation=val_mask, maxIter=self.config.max_iter,
                    maxAttempts=self.config.max_attempt)
        self.model = model
        self.add_data('model', self.model)


class GPz_v1(CatEstimator):
    """GPz_v1 estimator
    """
    name = "GPz_v1"
    config_options = CatEstimator.config_options.copy()
    config_options.update(zmin=SHARED_PARAMS,
                          zmax=SHARED_PARAMS,
                          nzbins=SHARED_PARAMS,
                          nondetect_val=SHARED_PARAMS,
                          mag_limits=SHARED_PARAMS,
                          bands=SHARED_PARAMS,
                          err_bands=SHARED_PARAMS,
                          ref_band=SHARED_PARAMS,
                          log_errors=Param(bool, True, msg="if true, take log of magnitude errors"))

    def __init__(self, args, comm=None):
        """ Constructor:
        Do CatEstimator specific initialization """
        CatEstimator.__init__(self, args, comm=comm)
        self.zgrid = None

    def _process_chunk(self, start, end, data, first):
        print(f"Process {self.rank} estimating GPz PZ PDF for rows {start:,} - {end:,}")
        test_array = _prepare_data(data, self.config.bands, self.config.err_bands,
                                   self.config.nondetect_val, self.config.mag_limits,
                                   self.config.log_errors)

        mu, totalV, modelV, noiseV, _ = self.model.predict(test_array)
        ens = qp.Ensemble(qp.stats.norm, data=dict(loc=mu, scale=totalV))
        zgrid = np.linspace(self.config.zmin, self.config.zmax, self.config.nzbins)
        zmode = ens.mode(grid=zgrid)
        ens.set_ancil(dict(zmode=zmode))
        self._do_chunk_output(ens, start, end, first)
