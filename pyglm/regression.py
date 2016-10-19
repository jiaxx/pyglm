"""
The "generalized linear models" of computational neuroscience
are ultimately nonlinear vector autoregressive models in
statistics. As the name suggests, the key component in these
models is a regression from inputs, x, to outputs, y.

When the outputs are discrete random variables, like spike
counts, we typically take the regression to be a generalized
linear model:

   y ~ p(mu(x), theta)
   mu(x) = f(w \dot x)

where 'p' is a discrete distribution, like the Poisson,
and 'f' is a "link" function that maps a linear function of
x to the parameters of 'p'. Hence the name "GLM" in
computational neuroscience.

Our contribution is a host of hierarchical models for the
weights of the GLM, along with an efficient Bayesian inference
algorithm for inferring the weights, 'w', under count observations.
Specifically, we build hierarchical sparse priors for the weights
and then leverage Polya-gamma augmentation to perform efficient
inference.

This module implements these sparse regressions.
"""
import abc
import numpy as np

from scipy.linalg import block_diag
from scipy.linalg.lapack import dpotrs

from pybasicbayes.abstractions import GibbsSampling
from pybasicbayes.util.stats import sample_gaussian, sample_discrete_from_log, sample_invgamma

from pyglm.utils.utils import logistic

class _SparseScalarRegressionBase(GibbsSampling):
    """
    Base class for the sparse regression.

    We assume the output dimension D = 1

    N: number of input groups
    B: input dimension for each group
    inputs: X \in R^{N \times B}
    outputs: y \in R^D

    model:

    y_d = \sum_{n=1}^N a_{d,n} * (w_{d,n} \dot x_n) + b_d + noise

    where:

    a_n \in {0,1}      is a binary indicator
    w_{d,n} \in R^B    is a weight matrix for group n
    x_n \in R^B        is the input for group n
    b \in R^D          is a bias vector

    hyperparameters:

    rho in [0,1]^N     probability of a_n for each group n
    mu_w in R^{DxNxB}  mean of weight matrices
    S_w in R^{DxNxBxB} covariance for each row of the the weight matrices
    mu_b in R^D        mean of the bias vector
    S_b in R^{DxD}     covariance of the bias vector

    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, N, B, D=1,
                 rho=0.5,
                 mu_w=0.0, S_w=1.0,
                 mu_b=0.0, S_b=1.0):
        self.D, self.N, self.B = D, N, B

        # Initialize the hyperparameters
        # Expand the means
        def expand_scalar(x, shp):
            if np.isscalar(x):
                x *= np.ones(shp)
            else:
                assert x.shape == shp
            return x

        self.rho = expand_scalar(rho, (N,))
        self.mu_w = expand_scalar(mu_w, (D, N, B))
        self.mu_b = expand_scalar(mu_b, (D,))

        # Expand the covariance matrices
        def expand_cov(c, shp):
            assert len(shp) >= 2
            assert shp[-2] == shp[-1]
            d = shp[-1]
            if np.isscalar(c):
                c = c * np.eye(d)
                tshp = np.array(shp)
                tshp[-2:] = 1
                c = np.tile(c, tshp)
            else:
                assert c.shape == shp

            return c

        self.S_w = expand_cov(S_w, (D, N, B, B))
        self.S_b = expand_cov(S_b, (D,D))

        # Compute information form parameters
        self.J_w = np.zeros((D, N, B, B))
        self.h_w = np.zeros((D, N, B))
        for d in range(D):
            for n in range(N):
                self.J_w[d,n] = np.linalg.inv(self.S_w[d,n])
                self.h_w[d,n] = self.J_w[d,n].dot(self.mu_w[d,n])

        self.J_b = np.linalg.inv(self.S_b)
        self.h_b = self.J_b.dot(self.mu_b)

        # Initialize the model parameters with a draw from the prior
        self.a = np.random.rand(N) < self.rho
        self.W = np.zeros((D,N,B))
        for d in range(D):
            for n in range(N):
                self.W[d,n] = np.random.multivariate_normal(self.mu_w[d,n], self.S_w[d,n])

        self.b = np.random.multivariate_normal(self.mu_b, self.S_b)

    @property
    def deterministic_sparsity(self):
        return np.all((self.rho < 1e-6) | (self.rho > 1-1e-6))

    @abc.abstractmethod
    def omega(self, X, y):
        """
        The "precision" of the observations y. For the standard
        homoskedastic Gaussian model, this is a function of model parameters.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def kappa(self, X, y):
        """
        The "normalized" observations, y. For the standard
        homoskedastic Gaussian model, this is the data times the precision.
        """
        raise NotImplementedError

    def _flatten_X(self, X):
        if X.ndim == 2:
            assert  X.shape[1] == self.N*self.B
        elif X.ndim == 3:
            X = np.reshape(X, (-1, self.N * self.B))
        else:
            raise Exception
        return X


    def extract_data(self, data):
        D, N, B = self.D, self.N, self.B
        assert D == 1, "Only supporting scalar regressions"

        assert isinstance(data, tuple) and len(data) == 2
        X, y = data
        T = X.shape[0]
        assert y.shape == (T, 1) or y.shape == (T,)

        # Reshape X such that it is T x NB
        X = self._flatten_X(X)
        return X, y

    def activation(self, X):
        D, N, B = self.D, self.N, self.B
        assert D == 1, "Only supporting scalar regression"
        X = self._flatten_X(X)

        W = np.reshape((self.a[:, None] * self.W[0]), (N * B,))
        b = self.b[0]
        return X.dot(W) + b

    @abc.abstractmethod
    def mean(self, X):
        """
        Return the expected value of y given X.
        """
        raise NotImplementedError

    def _prior_sufficient_statistics(self):
        """
        Compute the prior statistics (information form Gaussian
        potentials) for the complete set of weights and biases.
        """
        D, N, B = self.D, self.N, self.B
        assert D == 1, "Only supporting scalar regressions"

        J_prior = block_diag(*self.J_w[0], self.J_b)
        assert J_prior.shape == (N*B+1, N*B+1)

        h_prior = np.concatenate((self.h_w.ravel(), self.h_b.ravel()))
        assert h_prior.shape == (N*B+1,)
        return J_prior, h_prior

    def _lkhd_sufficient_statistics(self, datas):
        """
        Compute the likelihood statistics (information form Gaussian
        potentials) for each dataset.  Polya-gamma regressions will
        have to override this class.
        """
        D, N, B = self.D, self.N, self.B
        assert D ==1, "Only supporting scalar regressions"

        J_lkhd = np.zeros((N*B+1, N*B+1))
        h_lkhd = np.zeros(N*B+1)

        # Compute the posterior sufficient statistics
        for data in datas:
            assert isinstance(data, tuple)
            X, y = self.extract_data(data)
            T = X.shape[0]

            # Get the precision and the normalized observations
            omega = self.omega(X,y)
            assert omega.shape == (T,)
            kappa = self.kappa(X,y)
            assert kappa.shape == (T,)

            # Add the sufficient statistics to J_lkhd
            # The last row and column correspond to the
            # affine term
            XO = X * omega[:,None]
            J_lkhd[:N*B, :N*B] += XO.T.dot(X)
            Xsum = XO.sum(0)
            J_lkhd[:N*B,-1] += Xsum
            J_lkhd[-1,:N*B] += Xsum
            J_lkhd[-1,-1] += omega.sum()

            # Add the sufficient statisticcs to h_lkhd
            h_lkhd[:N*B] += kappa.T.dot(X)
            h_lkhd[-1] += kappa.sum()

        return J_lkhd, h_lkhd

    ### Gibbs sampling
    def resample(self, datas):
        # Compute the prior and posterior sufficient statistics of W
        J_prior, h_prior = self._prior_sufficient_statistics()
        J_lkhd, h_lkhd = self._lkhd_sufficient_statistics(datas)

        J_post = J_prior + J_lkhd
        h_post = h_prior + h_lkhd

        # Resample a
        if self.deterministic_sparsity:
            self.a = np.round(self.rho).astype(bool)
        else:
            self._collapsed_resample_a(J_prior, h_prior, J_post, h_post)

        # Resample weights
        self._resample_W(J_post, h_post)

    def _collapsed_resample_a(self, J_prior, h_prior, J_post, h_post):
        """
        """
        D, N, B, rho = self.D, self.N, self.B, self.rho
        assert D == 1, "Only supporting scalar regressions"


        perm = np.random.permutation(self.N)

        ml_prev = self._marginal_likelihood(J_prior, h_prior, J_post, h_post)
        for n in perm:
            # TODO: Check if rho is deterministic

            # Compute the marginal prob with and without A[m,n]
            lps = np.zeros(2)
            # We already have the marginal likelihood for the current value of a[m]
            # We just need to add the prior
            v_prev = self.a[n]
            lps[v_prev] += ml_prev
            lps[v_prev] += v_prev * np.log(rho[n]) + (1-v_prev) * np.log(1-rho[n])

            # Now compute the posterior stats for 1-v
            v_new = 1 - v_prev
            self.a[n] = v_new

            ml_new = self._marginal_likelihood(J_prior, h_prior, J_post, h_post)

            lps[v_new] += ml_new
            lps[v_new] += v_new * np.log(rho[n]) + (1-v_new) * np.log(1-rho[n])

            # Sample from the marginal probability
            # max_lps = max(lps[0], lps[1])
            # se_lps = np.sum(np.exp(lps-max_lps))
            # lse_lps = np.log(se_lps) + max_lps
            # ps = np.exp(lps - lse_lps)
            # v_smpl = np.random.rand() < ps[1]å
            v_smpl = sample_discrete_from_log(lps)
            self.a[n] = v_smpl

            # Cache the posterior stats and update the matrix objects
            if v_smpl != v_prev:
                ml_prev = ml_new


    def _resample_W(self, J_post, h_post):
        """
        Resample the weight of a connection (synapse)
        """
        D, N, B = self.D, self.N, self.B
        assert D == 1, "Only supporting scalar regressions"

        a = np.concatenate((np.repeat(self.a, self.B), [1])).astype(np.bool)
        Jp = J_post[np.ix_(a, a)]
        hp = h_post[a]

        # Sample in information form
        W = sample_gaussian(J=Jp, h=hp)

        # Set bias and weights
        self.W *= 0
        self.W[0, self.a, :] = W[:-1].reshape((-1,B))
        # self.W = np.reshape(W[:-1], (D,N,B))
        self.b = np.reshape(W[-1], (D,))


    def _marginal_likelihood(self, J_prior, h_prior, J_post, h_post):
        """
        Compute the marginal likelihood as the ratio of log normalizers
        """
        a = np.concatenate((np.repeat(self.a, self.B), [1])).astype(np.bool)

        # Extract the entries for which A=1
        J0 = J_prior[np.ix_(a, a)]
        h0 = h_prior[a]
        Jp = J_post[np.ix_(a, a)]
        hp = h_post[a]

        # This relates to the mean/covariance parameterization as follows
        # log |C| = log |J^{-1}| = -log |J|
        # and
        # mu^T C^{-1} mu = mu^T h
        #                = mu C^{-1} C h
        #                = h^T C h
        #                = h^T J^{-1} h
        # ml = 0
        # ml -= 0.5*np.linalg.slogdet(Jp)[1]
        # ml += 0.5*np.linalg.slogdet(J0)[1]
        # ml += 0.5*hp.dot(np.linalg.solve(Jp, hp))
        # ml -= 0.5*h0.T.dot(np.linalg.solve(J0, h0))

        # Now compute it even faster using the Cholesky!
        L0 = np.linalg.cholesky(J0)
        Lp = np.linalg.cholesky(Jp)

        ml = 0
        ml -= np.sum(np.log(np.diag(Lp)))
        ml += np.sum(np.log(np.diag(L0)))
        ml += 0.5*hp.T.dot(dpotrs(Lp, hp, lower=True)[0])
        ml -= 0.5*h0.T.dot(dpotrs(L0, h0, lower=True)[0])

        return ml

class SparseScalarRegression(_SparseScalarRegressionBase):
    """
    The standard case of a sparse regression with Gaussian observations.
    """
    def __init__(self, N, B, D=1,
                 a_0=2.0, b_0=2.0, eta=None,
                 **kwargs):
        super(SparseScalarRegression, self).__init__(N, B, D=D, **kwargs)

        # Initialize the noise model
        assert np.isscalar(a_0) and a_0 > 0
        assert np.isscalar(b_0) and a_0 > 0
        self.a_0, self.b_0 = a_0, b_0
        if eta is not None:
            assert np.isscalar(eta) and eta > 0
            self.eta = eta
        else:
            # Sample eta from its inverse gamma prior
            self.eta = sample_invgamma(self.a_0, self.b_0)

    def log_likelihood(self, x):
        D, N, B, eta = self.D, self.N, self.B, self.eta
        assert D == 1, "Only supporting scalar regressions"

        X, y = self.extract_data(x)
        return -0.5 * np.log(2*np.pi*eta) -0.5 * (y-self.mean(X))**2 / eta

    def rvs(self,size=[], X=None):
        D, N, B = self.D, self.N, self.B
        assert D == 1, "Only supporting scalar regressions"

        if X is None:
            assert isinstance(size, int)
            X = np.random.randn(size,N*B)

        X = self._flatten_X(X)
        T = X.shape[0]
        return self.mean(X) + np.sqrt(self.eta) * np.random.randn(T)

    def omega(self, X, y):
        T = X.shape[0]
        return 1./self.eta * np.ones(T)

    def kappa(self, X, y):
        return y / self.eta

    def resample(self, datas):
        super(SparseScalarRegression, self).resample(datas)
        self._resample_eta(datas)

    def mean(self, X):
        return self.activation(X)

    def _resample_eta(self, datas):
        D, N, B = self.D, self.N, self.B
        assert D == 1, "Only supporting scalar regressions"

        alpha = self.a_0
        beta = self.b_0
        for data in datas:
            X, y = self.extract_data(data)
            T = X.shape[0]

            alpha += T / 2.0
            beta += np.sum((y-self.mean(X))**2)

        self.eta = sample_invgamma(alpha, beta)

class _SparsePGRegressionBase(_SparseScalarRegressionBase):
    """
    Extend the sparse scalar regression to handle count observations
    by leveraging the Polya-gamma augmentation for logistic regression
    models. This supports the subclasses implemented below. Namely:
    - SparseBernoulliRegression
    - SparseBinomialRegression
    - SparseNegativeBinomialRegression
    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, N, B, **kwargs):
        super(_SparsePGRegressionBase, self).__init__(N, B, **kwargs)

        # Initialize Polya-gamma samplers
        import pypolyagamma as ppg
        num_threads = ppg.get_omp_num_threads()
        seeds = np.random.randint(2 ** 16, size=num_threads)
        self.ppgs = [ppg.PyPolyaGamma(seed) for seed in seeds]

    @abc.abstractmethod
    def a_func(self, y):
        raise NotImplementedError

    @abc.abstractmethod
    def b_func(self, y):
        raise NotImplementedError

    @abc.abstractmethod
    def c_func(self, y):
        raise NotImplementedError

    def log_likelihood(self, x):
        X, y = self.extract_data(x)
        psi = self.activation(X)
        return np.log(self.c_func(y)) + self.a_func(y) * psi - self.b_func(y) * np.log1p(np.exp(psi))

    def omega(self, X, y):
        """
        In the Polya-gamma augmentation, the precision is
        given by an auxiliary variable that we must sample
        """
        import pypolyagamma as ppg
        psi = self.activation(X)
        omega = np.zeros(y.size)
        ppg.pgdrawvpar(self.ppgs,
                       self.b_func(y).ravel(),
                       psi.ravel(),
                       omega)
        return omega.reshape(y.shape)

    def kappa(self, X, y):
        return self.a_func(y) - self.b_func(y) / 2.0


class SparseBernoulliRegression(_SparsePGRegressionBase):
    def a_func(self, data):
        return data

    def b_func(self, data):
        return np.ones_like(data, dtype=np.float)

    def c_func(self, data):
        return 1.0

    def mean(self, X):
        psi = self.activation(X)
        return logistic(psi)

    def rvs(self, X=None, size=[], return_xy=False):
        if X is None:
            assert isinstance(size, int)
            X = np.random.randn(size, self.N*self.B)

        X = self._flatten_X(X)
        p = self.mean(X)
        y = np.random.rand(*p.shape) < p

        return (X, y) if return_xy else y