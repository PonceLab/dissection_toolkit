import time
import math
import numpy as np
from numpy import sqrt, zeros, floor, log, log2, eye, exp, linspace, logspace, log10, mean, std
from numpy.linalg import norm
from numpy.random import randn
from circuit_toolkit.geometry_utils import ExpMap, renormalize, ang_dist, SLERP
import torch

class CholeskyCMAES:
    """ Note this is a variant of CMAES Cholesky suitable for high dimensional optimization"""
    def __init__(self, space_dimen, population_size=None, init_sigma=3.0, init_code=None, Aupdate_freq=10,
                 maximize=True, random_seed=None, optim_params={}):
        N = space_dimen
        self.space_dimen = space_dimen
        # Overall control parameter
        self.maximize = maximize  # if the program is to maximize or to minimize
        # Strategy parameter setting: Selection
        if population_size is None:
            self.lambda_ = int(4 + floor(3 * log2(N)))  # population size, offspring number
            # the relation between dimension and population size.
        else:
            self.lambda_ = population_size  # use custom specified population size
        mu = self.lambda_ / 2  # number of parents/points for recombination
        #  Select half the population size as parents
        weights = log(mu + 1 / 2) - (log(np.arange(1, 1 + floor(mu))))  # muXone array for weighted recombination
        self.mu = int(floor(mu))
        self.weights = weights / sum(weights)  # normalize recombination weights array
        mueff = self.weights.sum() ** 2 / sum(self.weights ** 2)  # variance-effectiveness of sum w_i x_i
        self.weights.shape = (1, -1)  # Add the 1st dim 1 to the weights mat
        self.mueff = mueff  # add to class variable
        self.sigma = init_sigma  # Note by default, sigma is None here.
        print("Space dimension: %d, Population size: %d, Select size:%d, Optimization Parameters:\nInitial sigma: %.3f"
              % (self.space_dimen, self.lambda_, self.mu, self.sigma))
        # Strategy parameter setting: Adaptation
        self.cc = 4 / (N + 4)  # defaultly  0.0009756
        self.cs = sqrt(mueff) / (sqrt(mueff) + sqrt(N))  # 0.0499
        self.c1 = 2 / (N + sqrt(2)) ** 2  # 1.1912701410022985e-07
        if "cc" in optim_params.keys():  # if there is outside value for these parameter, overwrite them
            self.cc = optim_params["cc"]
        if "cs" in optim_params.keys():
            self.cs = optim_params["cs"]
        if "c1" in optim_params.keys():
            self.c1 = optim_params["c1"]
        self.damps = 1 + self.cs + 2 * max(0, sqrt((mueff - 1) / (N + 1)) - 1)  # damping for sigma usually  close to 1

        print("cc=%.3f, cs=%.3f, c1=%.3f damps=%.3f" % (self.cc, self.cs, self.c1, self.damps))
        if init_code is not None:
            self.init_x = np.asarray(init_code)
            self.init_x.shape = (1, N)
        else:
            self.init_x = None  # FIXED Nov. 1st
        self.xmean = zeros((1, N))
        self.xold = zeros((1, N))
        # Initialize dynamic (internal) strategy parameters and constants
        self.pc = zeros((1, N))
        self.ps = zeros((1, N))  # evolution paths for C and sigma
        self.A = eye(N, N)  # covariant matrix is represent by the factors A * A '=C
        self.Ainv = eye(N, N)

        self.eigeneval = 0  # track update of B and D
        self.counteval = 0
        if Aupdate_freq is None:
            self.update_crit = self.lambda_ / self.c1 / N / 10
        else:
            self.update_crit = Aupdate_freq * self.lambda_
        self.chiN = sqrt(N) * (1 - 1 / (4 * N) + 1 / (21 * N ** 2))
        # expectation of ||N(0,I)|| == norm(randn(N,1)) in 1/N expansion formula
        self._istep = 0

    def get_init_pop(self):
        return self.init_x

    def step_simple(self, scores, codes, verbosity=1):
        """ Taking scores and codes to return new codes, without generating images
        Used in cases when the images are better handled in outer objects like Experiment object
        """
        # Note it's important to decide which variable is to be saved in the `Optimizer` object
        # Note to confirm with other code, this part is transposed.
        # set short name for everything to simplify equations
        N = self.space_dimen
        # lambda_, mu, mueff, chiN = self.lambda_, self.mu, self.mueff, self.chiN
        # cc, cs, c1, damps = self.cc, self.cs, self.c1, self.damps
        # sigma, A, Ainv, ps, pc, = self.sigma, self.A, self.Ainv, self.ps, self.pc,
        # Sort by fitness and compute weighted mean into xmean
        if self.maximize is False:
            code_sort_index = np.argsort( scores)  # add - operator it will do maximization.
        else:
            code_sort_index = np.argsort(-scores)
        # scores = scores[code_sort_index]  # Ascending order. minimization
        if self._istep == 0:
            # Population Initialization: if without initialization, the first xmean is evaluated from weighted average all the natural images
            if self.init_x is None:
                select_n = len(code_sort_index[0:self.mu])
                temp_weight = self.weights[:, :select_n] / np.sum(self.weights[:, :select_n]) # in case the codes is not enough
                self.xmean = temp_weight @ codes[code_sort_index[0:self.mu], :]
            else:
                self.xmean = self.init_x
        else:
            self.xold = self.xmean
            self.xmean = self.weights @ codes[code_sort_index[0:self.mu], :]  # Weighted recombination, new mean value
            # Cumulation statistics through steps: Update evolution paths
            randzw = self.weights @ self.randz[code_sort_index[0:self.mu], :]
            self.ps = (1 - self.cs) * self.ps + sqrt(self.cs * (2 - self.cs) * self.mueff) * randzw
            self.pc = (1 - self.cc) * self.pc + sqrt(self.cc * (2 - self.cc) * self.mueff) * randzw @ self.A
            # Adapt step size sigma
            self.sigma = self.sigma * exp((self.cs / self.damps) * (norm(self.ps) / self.chiN - 1))
            # self.sigma = self.sigma * exp((self.cs / self.damps) * (norm(ps) / self.chiN - 1))
            if verbosity: 
                print("sigma: %.2f" % self.sigma)
            # Update A and Ainv with search path
            if self.counteval - self.eigeneval > self.update_crit:  # to achieve O(N ^ 2) do decomposition less frequently
                self.eigeneval = self.counteval
                t1 = time.time()
                v = self.pc @ self.Ainv
                normv = v @ v.T
                # Directly update the A Ainv instead of C itself
                self.A = sqrt(1 - self.c1) * self.A + sqrt(1 - self.c1) / normv * (
                            sqrt(1 + normv * self.c1 / (1 - self.c1)) - 1) * v.T @ self.pc  # FIXME, dimension error, # FIXED aug.13th
                self.Ainv = 1 / sqrt(1 - self.c1) * self.Ainv - 1 / sqrt(1 - self.c1) / normv * (
                            1 - 1 / sqrt(1 + normv * self.c1 / (1 - self.c1))) * self.Ainv @ v.T @ v
                t2 = time.time()
                if verbosity: 
                    print("A, Ainv update! Time cost: %.2f s" % (t2 - t1))
        # Generate new sample by sampling from Gaussian distribution
        # new_samples = zeros((self.lambda_, N))
        self.randz = randn(self.lambda_, N)  # save the random number for generating the code.
        new_samples = self.xmean + self.sigma * self.randz @ self.A
        self.counteval += self.lambda_
        # for k in range(self.lambda_):
        #     new_samples[k:k + 1, :] = self.xmean + sigma * (self.randz[k, :] @ A)  # m + sig * Normal(0,C)
        #     # Clever way to generate multivariate gaussian!!
        #     # Stretch the guassian hyperspher with D and transform the
        #     # ellipsoid by B mat linear transform between coordinates
        #     self.counteval += 1
        # self.sigma, self.A, self.Ainv, self.ps, self.pc = sigma, A, Ainv, ps, pc,
        self._istep += 1
        return new_samples

def rankweight(lambda_, mu=None):
    """ Rank weight inspired by CMA-ES code
    mu is the cut off number, how many samples will be kept while `lambda_ - mu` will be ignore
    """
    if mu is None:
        mu = lambda_ / 2  # number of parents/points for recombination
        #  Defaultly Select half the population size as parents
    weights = zeros(int(lambda_))
    mu_int = int(floor(mu))
    weights[:mu_int] = log(mu + 1 / 2) - (log(np.arange(1, 1 + floor(mu))))  # muXone array for weighted recombination
    weights = weights / sum(weights)
    return weights


class CholeskyCMAES_torch:
    """Note this is a variant of CMAES Cholesky suitable for high-dimensional optimization."""

    def __init__(self, space_dimen, population_size=None, init_sigma=3.0, init_code=None, Aupdate_freq=10,
                 maximize=True, random_seed=None, optim_params={}, device='cpu', dtype=torch.float32):
        N = space_dimen
        self.space_dimen = space_dimen
        self.device = torch.device(device)
        self.dtype = dtype
        # Overall control parameter
        self.maximize = maximize  # if the program is to maximize or to minimize

        # Strategy parameter setting: Selection
        if population_size is None:
            self.lambda_ = int(4 + math.floor(3 * math.log2(N)))  # population size, offspring number
        else:
            self.lambda_ = population_size  # use custom specified population size

        mu = self.lambda_ / 2  # number of parents/points for recombination
        self.mu = int(math.floor(mu))

        # Compute weights for recombination
        weights = torch.log(torch.tensor(mu + 0.5, device=self.device)) - \
                  torch.log(torch.arange(1, self.mu + 1, device=self.device, dtype=self.dtype))
        self.weights = weights / torch.sum(weights)  # normalize recombination weights array
        self.weights = self.weights.reshape(1, -1)  # Add the 1st dim 1 to the weights matrix

        mueff = self.weights.sum() ** 2 / torch.sum(self.weights ** 2)
        self.mueff = mueff.item()

        self.sigma = init_sigma
        print("Space dimension: %d, Population size: %d, Select size:%d, Optimization Parameters:\nInitial sigma: %.3f"
              % (self.space_dimen, self.lambda_, self.mu, self.sigma))

        # Strategy parameter setting: Adaptation
        self.cc = 4 / (N + 4)
        self.cs = math.sqrt(self.mueff) / (math.sqrt(self.mueff) + math.sqrt(N))
        self.c1 = 2 / (N + math.sqrt(2)) ** 2

        # Overwrite parameters if provided
        self.cc = optim_params.get("cc", self.cc)
        self.cs = optim_params.get("cs", self.cs)
        self.c1 = optim_params.get("c1", self.c1)

        self.damps = 1 + self.cs + 2 * max(0, math.sqrt((self.mueff - 1) / (N + 1)) - 1)
        print("cc=%.3f, cs=%.3f, c1=%.3f, damps=%.3f" % (self.cc, self.cs, self.c1, self.damps))

        if init_code is not None:
            self.init_x = torch.tensor(init_code, device=self.device, dtype=self.dtype).reshape(1, N)
        else:
            self.init_x = None

        self.xmean = torch.zeros((1, N), device=self.device, dtype=self.dtype)
        self.xold = torch.zeros((1, N), device=self.device, dtype=self.dtype)
        self.pc = torch.zeros((1, N), device=self.device, dtype=self.dtype)
        self.ps = torch.zeros((1, N), device=self.device, dtype=self.dtype)
        self.A = torch.eye(N, device=self.device, dtype=self.dtype)
        self.Ainv = torch.eye(N, device=self.device, dtype=self.dtype)

        self.eigeneval = 0
        self.counteval = 0
        if Aupdate_freq is None:
            self.update_crit = self.lambda_ / self.c1 / N / 10
        else:
            self.update_crit = Aupdate_freq * self.lambda_

        self.chiN = math.sqrt(N) * (1 - 1 / (4 * N) + 1 / (21 * N ** 2))
        self._istep = 0

    def get_init_pop(self):
        return self.init_x

    def step_simple(self, scores, codes, verbosity=1):
        """Taking scores and codes to return new codes, without generating images."""
        N = self.space_dimen

        # Ensure scores and codes are tensors on the correct device
        if not isinstance(scores, torch.Tensor):
            scores = torch.tensor(scores, device=self.device, dtype=self.dtype)
        if not isinstance(codes, torch.Tensor):
            codes = torch.tensor(codes, device=self.device, dtype=self.dtype)

        # Sort by fitness and compute weighted mean into xmean
        if self.maximize:
            code_sort_index = torch.argsort(-scores)
        else:
            code_sort_index = torch.argsort(scores)

        if self._istep == 0:
            # Population Initialization
            if self.init_x is None:
                select_n = len(code_sort_index[:self.mu])
                temp_weight = self.weights[:, :select_n] / torch.sum(self.weights[:, :select_n])
                self.xmean = temp_weight @ codes[code_sort_index[:self.mu], :]
            else:
                self.xmean = self.init_x
        else:
            self.xold = self.xmean.clone()
            self.xmean = self.weights @ codes[code_sort_index[:self.mu], :]

            # Update evolution paths
            randzw = self.weights @ self.randz[code_sort_index[:self.mu], :]
            self.ps = (1 - self.cs) * self.ps + math.sqrt(self.cs * (2 - self.cs) * self.mueff) * randzw
            self.pc = (1 - self.cc) * self.pc + math.sqrt(self.cc * (2 - self.cc) * self.mueff) * (randzw @ self.A)

            # Adapt step size sigma
            self.sigma = self.sigma * math.exp((self.cs / self.damps) * (torch.norm(self.ps) / self.chiN - 1))
            if verbosity:
                print("sigma: %.2f" % self.sigma)

            # Update A and Ainv with search path
            if self.counteval - self.eigeneval > self.update_crit:
                self.eigeneval = self.counteval
                t1 = time.time()
                v = self.pc @ self.Ainv
                normv = (v @ v.T).item()
                sqrt_term = math.sqrt(1 + normv * self.c1 / (1 - self.c1))
                self.A = math.sqrt(1 - self.c1) * self.A + \
                         (math.sqrt(1 - self.c1) / normv) * (sqrt_term - 1) * v.T @ self.pc
                self.Ainv = (1 / math.sqrt(1 - self.c1)) * self.Ainv - \
                            (1 / (math.sqrt(1 - self.c1) * normv)) * (1 - 1 / sqrt_term) * (self.Ainv @ v.T @ v)
                t2 = time.time()
                if verbosity:
                    print("A, Ainv update! Time cost: %.2f s" % (t2 - t1))

        # Generate new samples
        self.randz = torch.randn(self.lambda_, N, device=self.device, dtype=self.dtype)
        new_samples = self.xmean + self.sigma * (self.randz @ self.A)
        self.counteval += self.lambda_
        self._istep += 1
        return new_samples


class CholeskyCMAES_torch_noCMA:
    """Note this is a variant of CMAES Cholesky suitable for high-dimensional optimization."""

    def __init__(self, space_dimen, population_size=None, init_sigma=3.0, init_code=None, Aupdate_freq=10,
                 maximize=True, random_seed=None, optim_params={}, device='cpu'):
        N = space_dimen
        self.space_dimen = space_dimen
        self.device = torch.device(device)
        # Overall control parameter
        self.maximize = maximize  # if the program is to maximize or to minimize

        # Strategy parameter setting: Selection
        if population_size is None:
            self.lambda_ = int(4 + math.floor(3 * math.log2(N)))  # population size, offspring number
        else:
            self.lambda_ = population_size  # use custom specified population size

        mu = self.lambda_ / 2  # number of parents/points for recombination
        self.mu = int(math.floor(mu))

        # Compute weights for recombination
        weights = torch.log(torch.tensor(mu + 0.5, device=self.device)) - \
                  torch.log(torch.arange(1, self.mu + 1, device=self.device, dtype=torch.float32))
        self.weights = weights / torch.sum(weights)  # normalize recombination weights array
        self.weights = self.weights.reshape(1, -1)  # Add the 1st dim 1 to the weights matrix

        mueff = self.weights.sum() ** 2 / torch.sum(self.weights ** 2)
        self.mueff = mueff.item()

        self.sigma = init_sigma
        print("Space dimension: %d, Population size: %d, Select size:%d, Optimization Parameters:\nInitial sigma: %.3f"
              % (self.space_dimen, self.lambda_, self.mu, self.sigma))

        # Strategy parameter setting: Adaptation
        self.cc = 4 / (N + 4)
        self.cs = math.sqrt(self.mueff) / (math.sqrt(self.mueff) + math.sqrt(N))
        self.c1 = 2 / (N + math.sqrt(2)) ** 2

        # Overwrite parameters if provided
        self.cc = optim_params.get("cc", self.cc)
        self.cs = optim_params.get("cs", self.cs)
        self.c1 = optim_params.get("c1", self.c1)

        self.damps = 1 + self.cs + 2 * max(0, math.sqrt((self.mueff - 1) / (N + 1)) - 1)
        print("cc=%.3f, cs=%.3f, c1=%.3f, damps=%.3f" % (self.cc, self.cs, self.c1, self.damps))

        if init_code is not None:
            self.init_x = torch.tensor(init_code, device=self.device, dtype=torch.float32).reshape(1, N)
        else:
            self.init_x = None

        self.xmean = torch.zeros((1, N), device=self.device)
        self.xold = torch.zeros((1, N), device=self.device)
        self.pc = torch.zeros((1, N), device=self.device)
        self.ps = torch.zeros((1, N), device=self.device)
        # self.A = torch.eye(N, device=self.device)
        # self.Ainv = torch.eye(N, device=self.device)

        self.eigeneval = 0
        self.counteval = 0
        if Aupdate_freq is None:
            self.update_crit = self.lambda_ / self.c1 / N / 10
        else:
            self.update_crit = Aupdate_freq * self.lambda_

        self.chiN = math.sqrt(N) * (1 - 1 / (4 * N) + 1 / (21 * N ** 2))
        self._istep = 0

    def get_init_pop(self):
        return self.init_x

    def step_simple(self, scores, codes, verbosity=1):
        """Taking scores and codes to return new codes, without generating images."""
        N = self.space_dimen

        # Ensure scores and codes are tensors on the correct device
        if not isinstance(scores, torch.Tensor):
            scores = torch.tensor(scores, device=self.device, dtype=torch.float32)
        if not isinstance(codes, torch.Tensor):
            codes = torch.tensor(codes, device=self.device, dtype=torch.float32)

        # Sort by fitness and compute weighted mean into xmean
        if self.maximize:
            code_sort_index = torch.argsort(-scores)
        else:
            code_sort_index = torch.argsort(scores)

        if self._istep == 0:
            # Population Initialization
            if self.init_x is None:
                select_n = len(code_sort_index[:self.mu])
                temp_weight = self.weights[:, :select_n] / torch.sum(self.weights[:, :select_n])
                self.xmean = temp_weight @ codes[code_sort_index[:self.mu], :]
            else:
                self.xmean = self.init_x
        else:
            self.xold = self.xmean.clone()
            self.xmean = self.weights @ codes[code_sort_index[:self.mu], :]

            # Update evolution paths
            randzw = self.weights @ self.randz[code_sort_index[:self.mu], :]
            self.ps = (1 - self.cs) * self.ps + math.sqrt(self.cs * (2 - self.cs) * self.mueff) * randzw
            self.pc = (1 - self.cc) * self.pc + math.sqrt(self.cc * (2 - self.cc) * self.mueff) * (randzw) #  @ self.A

            # Adapt step size sigma
            self.sigma = self.sigma * math.exp((self.cs / self.damps) * (torch.norm(self.ps) / self.chiN - 1))
            if verbosity:
                print("sigma: %.2f" % self.sigma)

            # # Update A and Ainv with search path
            # if self.counteval - self.eigeneval > self.update_crit:
            #     self.eigeneval = self.counteval
            #     t1 = time.time()
            #     v = self.pc @ self.Ainv
            #     normv = (v @ v.T).item()
            #     sqrt_term = math.sqrt(1 + normv * self.c1 / (1 - self.c1))
            #     self.A = math.sqrt(1 - self.c1) * self.A + \
            #              (math.sqrt(1 - self.c1) / normv) * (sqrt_term - 1) * v.T @ self.pc
            #     self.Ainv = (1 / math.sqrt(1 - self.c1)) * self.Ainv - \
            #                 (1 / (math.sqrt(1 - self.c1) * normv)) * (1 - 1 / sqrt_term) * (self.Ainv @ v.T @ v)
            #     t2 = time.time()
            #     if verbosity:
            #         print("A, Ainv update! Time cost: %.2f s" % (t2 - t1))

        # Generate new samples
        self.randz = torch.randn(self.lambda_, N, device=self.device)
        new_samples = self.xmean + self.sigma * (self.randz) # @ self.A
        self.counteval += self.lambda_
        self._istep += 1
        return new_samples


def rankweight_torch(lambda_, mu=None, device='cpu'):
    """Rank weight inspired by CMA-ES code."""
    if mu is None:
        mu = lambda_ / 2  # number of parents/points for recombination
    weights = torch.zeros(int(lambda_), device=device)
    mu_int = int(math.floor(mu))
    weights[:mu_int] = torch.log(torch.tensor(mu + 0.5, device=device)) - \
                       torch.log(torch.arange(1, mu_int + 1, device=device, dtype=torch.float32))
    weights = weights / weights.sum()
    return weights


class ZOHA_Sphere_lr_euclid:
    def __init__(self, space_dimen, population_size=40, select_size=20, lr=1.5, \
                 maximize=True, rankweight=True, rankbasis=False, sphere_norm=300):
        self.dimen = space_dimen   # dimension of input space
        self.B = population_size   # population batch size
        self.select_cutoff = select_size
        self.sphere_norm = sphere_norm
        self.lr = lr  # learning rate (step size) of moving along gradient

        self.tang_codes = zeros((self.B, self.dimen))
        self.grad = zeros((1, self.dimen))  # estimated gradient
        self.innerU = zeros((self.B, self.dimen))  # inner random vectors with covariance matrix Id
        self.outerV = zeros ((self.B, self.dimen))  # outer random vectors with covariance matrix H^{-1}, equals innerU @ H^{-1/2}
        self.xcur = zeros((1, self.dimen)) # current base point
        self.xnew = zeros((1, self.dimen)) # new base point

        self.istep = -1  # step counter
        # self.counteval = 0
        self.maximize = maximize # maximize / minimize the function
        self.rankweight = rankweight# Switch between using raw score as weight VS use rank weight as score
        self.rankbasis = rankbasis # Ranking basis or rank weights only
        # opts # object to store options for the future need to examine or tune

    def get_init_pop(self):
        return renormalize(np.random.randn(self.B, self.dimen), self.sphere_norm)

    def lr_schedule(self, n_gen=100, mode="inv", lim=(50, 7.33) ,):
        # note this normalize to the expected norm of a N dimensional Gaussian
        if mode == "inv":
            self.mulist = 15 + 1 / (0.0017 * np.arange(1, n_gen +1) + 0.0146);
            # self.opts.mu_init = self.mulist[0]
            # self.opts.mu_final = self.mulist[-1]
            self.mulist = self.mulist / 180 * np.pi / sqrt(self.dimen)
            self.mu_init = self.mulist[0]; self.mu_final = self.mulist[-1]
        else:
            self.mu_init = lim[0]
            self.mu_final = lim[1]
            if mode == "lin":
                self.mulist = linspace(self.mu_init, self.mu_final, n_gen) / 180 * np.pi / sqrt(self.dimen)
            elif mode == "exp":
                self.mulist = logspace(log10(self.mu_init), log10(self.mu_final), n_gen) / 180 * np.pi / sqrt(self.dimen)

    def step_simple(self, scores, codes, verbosity=1):
        N = self.dimen;
        if verbosity:
            print('Gen %d max score %.3f, mean %.3f, std %.3f\n ' %(self.istep, max(scores), mean(scores), std(scores) ))
        if self.istep == -1:
            # Population Initialization: if without initialization, the first xmean is evaluated from weighted average all the natural images
            if verbosity:
                print('First generation')
            self.xcur = codes[0:1, :]
            if not self.rankweight: # use the score difference as weight
                # B normalizer should go here larger cohort of codes gives more estimates
                weights = (scores - scores[0]) / self.B # / self.mu
            else:  # use a function of rank as weight, not really gradient.
                if not self.maximize: # note for weighted recombination, the maximization flag is here.
                    code_rank = scores.argsort().argsort() # find rank of ascending order
                else:
                    code_rank = (-scores).argsort().argsort() # find rank of descending order
                # Note the weights here are internally normalized s.t. sum up to 1, no need to normalize more.
                raw_weights = rankweight(len(code_rank))
                weights = raw_weights[code_rank] # map the rank to the corresponding weight of recombination
                # Consider the basis in our rank! but the weight will be wasted as we don't use it.

            w_mean = weights[np.newaxis,:] @ codes # mean in the euclidean space
            self.xnew = w_mean / norm(w_mean) * self.sphere_norm # project it back to shell.
        else:
            self.xcur = codes[0:1, :]
            if not self.rankweight: # use the score difference as weight
                # B normalizer should go here larger cohort of codes gives more estimates
                weights = (scores - scores[0]) / self.B; # / self.mu
            else:  # use a function of rank as weight, not really gradient.
                if not self.rankbasis: # if false, then exclude the first basis vector from rank (thus it receive no weights.)
                    rankedscore = scores[1:]
                else:
                    rankedscore = scores
                if not self.maximize: # note for weighted recombination, the maximization flag is here.
                    code_rank = rankedscore.argsort().argsort() # find rank of ascending order
                else:
                    code_rank = (-rankedscore).argsort().argsort() # find rank of descending order
                # Note the weights here are internally normalized s.t. sum up to 1, no need to normalize more.
                raw_weights = rankweight(len(code_rank), mu=self.select_cutoff)
                weights = raw_weights[code_rank] # map the rank to the corresponding weight of recombination
                # Consider the basis in our rank! but the weight will be wasted as we don't use it.
                if not self.rankbasis:
                    weights = np.append(0, weights) # the weight of the basis vector will do nothing! as the deviation will be nothing
            # estimate gradient from the codes and scores
            # assume weights is a row vector
            w_mean = weights[np.newaxis,:] @ codes # mean in the euclidean space
            w_mean = w_mean / norm(w_mean) * self.sphere_norm # rescale, project it back to shell.
            self.xnew = SLERP(self.xcur, w_mean, self.lr) # use lr to spherical extrapolate
            ang_basis_to_samp = ang_dist(codes, self.xnew)
            if verbosity:
                print("Step size %.3f, multip learning rate %.3f, " % (ang_dist(self.xcur, self.xnew), ang_dist(self.xcur, self.xnew) * self.lr));
                print("New basis ang to last samples mean %.3f(%.3f), min %.3f" % (mean(ang_basis_to_samp), std(ang_basis_to_samp), min(ang_basis_to_samp)));

        # Generate new sample by sampling from Gaussian distribution
        self.tang_codes = zeros((self.B, N))  # Tangent vectors of exploration
        self.innerU = randn(self.B, N)  # Isotropic gaussian distributions
        self.outerV = self.innerU # H^{-1/2}U, more transform could be applied here!
        self.outerV = self.outerV - (self.outerV @ self.xnew.T) @ self.xnew / norm(self.xnew) ** 2 # orthogonal projection to xnew's tangent plane.
        mu = self.mulist[self.istep + 1] if self.istep < len(self.mulist) - 1 else self.mulist[-1]
        new_samples = zeros((self.B + 1, N))
        new_samples[0, :] = self.xnew
        self.tang_codes = mu * self.outerV # m + sig * Normal(0,C)
        new_samples[1:, :] = ExpMap(self.xnew, self.tang_codes)
        if verbosity:
            print("Current Exploration %.1f deg" % (mu * sqrt(self.dimen - 1) / np.pi * 180))
        # new_ids = [];
        # for k in range(new_samples.shape[0]):
        #     new_ids = [new_ids, sprintf("gen%03d_%06d", self.istep+1, self.counteval)];
        #     self.counteval = self.counteval + 1;
        self.istep = self.istep + 1
        new_samples = renormalize(new_samples, self.sphere_norm)
        return new_samples


class HessCMAES:
    """ Note this is a variant of CMAES Cholesky suitable for high dimensional optimization"""
    def __init__(self, space_dimen, population_size=None, cutoff=None, init_sigma=3.0, init_code=None, Aupdate_freq=10, maximize=True, random_seed=None, optim_params={}):
        if cutoff is None: cutoff = space_dimen
        N = cutoff
        self.code_len = space_dimen
        self.space_dimen = cutoff # Overall control parameter
        self.maximize = maximize  # if the program is to maximize or to minimize
        # Strategy parameter setting: Selection
        if population_size is None:
            self.lambda_ = int(4 + floor(3 * log2(N)))  # population size, offspring number
            # the relation between dimension and population size.
        else:
            self.lambda_ = population_size  # use custom specified population size
        mu = self.lambda_ / 2  # number of parents/points for recombination
        #  Select half the population size as parents
        weights = log(mu + 1 / 2) - (log(np.arange(1, 1 + floor(mu))))  # muXone array for weighted recombination
        self.mu = int(floor(mu))
        self.weights = weights / sum(weights)  # normalize recombination weights array
        mueff = self.weights.sum() ** 2 / sum(self.weights ** 2)  # variance-effectiveness of sum w_i x_i
        self.weights.shape = (1, -1)  # Add the 1st dim 1 to the weights mat
        self.mueff = mueff  # add to class variable
        self.sigma = init_sigma  # Note by default, sigma is None here.
        print("Space dimension: %d, Population size: %d, Select size:%d, Optimization Parameters:\nInitial sigma: %.3f"
              % (self.space_dimen, self.lambda_, self.mu, self.sigma))
        # Strategy parameter settiself.weightsng: Adaptation
        self.cc = 4 / (N + 4)  # defaultly  0.0009756
        self.cs = sqrt(mueff) / (sqrt(mueff) + sqrt(N))  # 0.0499
        self.c1 = 2 / (N + sqrt(2)) ** 2  # 1.1912701410022985e-07
        if "cc" in optim_params.keys():  # if there is outside value for these parameter, overwrite them
            self.cc = optim_params["cc"]
        if "cs" in optim_params.keys():
            self.cs = optim_params["cs"]
        if "c1" in optim_params.keys():
            self.c1 = optim_params["c1"]
        self.damps = 1 + self.cs + 2 * max(0, sqrt((mueff - 1) / (N + 1)) - 1)  # damping for sigma usually  close to 1
        print("cc=%.3f, cs=%.3f, c1=%.3f damps=%.3f" % (self.cc, self.cs, self.c1, self.damps))
        if init_code is not None:
            self.init_x = np.asarray(init_code).reshape(1,-1)
            # if self.init_x.shape[1] == space_dimen:
            #     self.projection = True
            # elif self.init_x.shape[1] == cutoff:
            #     self.projection = False
            # else:
            #     raise ValueError
        else:
            self.init_x = None  # FIXED Nov. 1st
        self.xmean = zeros((1, N))
        self.xold = zeros((1, N))
        # Initialize dynamic (internal) strategy parameters and constants
        self.pc = zeros((1, space_dimen))
        self.ps = zeros((1, N))  # evolution paths for C and sigma
        self.A = eye(N, space_dimen, )  # covariant matrix is represent by the factors A * A '=C
        self.Ainv = eye(space_dimen, N, )

        self.eigeneval = 0  # track update of B and D
        self.counteval = 0
        if Aupdate_freq is None:
            self.update_crit = self.lambda_ / self.c1 / N / 10
        else:
            self.update_crit = Aupdate_freq * self.lambda_
        self.chiN = sqrt(N) * (1 - 1 / (4 * N) + 1 / (21 * N ** 2))
        # expectation of ||N(0,I)|| == norm(randn(N,1)) in 1/N expansion formula
        self._istep = 0

    def set_Hessian(self, eigvals, eigvects, cutoff=None, expon=1/2.5):
        cutoff = self.space_dimen
        self.eigvals = eigvals[:cutoff]
        self.eigvects = eigvects[:, :cutoff]
        self.scaling = self.eigvals ** (-expon)
        self.A = self.scaling[:,np.newaxis] * self.eigvects.T # cutoff by spacedimen
        self.Ainv = (1 / self.scaling[np.newaxis,:]) * self.eigvects # spacedimen by cutoff
        # if self.projection:
        #     self.init_x = self.init_x @ self.Ainv

    def step_simple(self, scores, codes):
        """ Taking scores and codes to return new codes, without generating images
        Used in cases when the images are better handled in outer objects like Experiment object
        """
        # Note it's important to decide which variable is to be saved in the `Optimizer` object
        # Note to confirm with other code, this part is transposed.
        # set short name for everything to simplify equations
        N = self.space_dimen
        lambda_, mu, mueff, chiN = self.lambda_, self.mu, self.mueff, self.chiN
        cc, cs, c1, damps = self.cc, self.cs, self.c1, self.damps
        sigma, A, Ainv, ps, pc, = self.sigma, self.A, self.Ainv, self.ps, self.pc,
        # Sort by fitness and compute weighted mean into xmean
        if self.maximize is False:
            code_sort_index = np.argsort( scores)  # add - operator it will do maximization.
        else:
            code_sort_index = np.argsort(-scores)
        # scores = scores[code_sort_index]  # Ascending order. minimization
        if self._istep == 0:
            # Population Initialization: if without initialization, the first xmean is evaluated from weighted average all the natural images
            if self.init_x is None:
                select_n = len(code_sort_index[0:mu])
                temp_weight = self.weights[:, :select_n] / np.sum(self.weights[:, :select_n]) # in case the codes is not enough
                self.xmean = temp_weight @ codes[code_sort_index[0:mu], :]
            else:
                self.xmean = self.init_x
        else:
            self.xold = self.xmean
            self.xmean = self.weights @ codes[code_sort_index[0:mu], :]  # Weighted recombination, new mean value
            # Cumulation statistics through steps: Update evolution paths
            randzw = self.weights @ self.randz[code_sort_index[0:mu], :]
            ps = (1 - cs) * ps + sqrt(cs * (2 - cs) * mueff) * randzw
            pc = (1 - cc) * pc + sqrt(cc * (2 - cc) * mueff) * randzw @ A
            # Adapt step size sigma
            sigma = sigma * exp((cs / damps) * (norm(ps) / chiN - 1))
            # self.sigma = self.sigma * exp((self.cs / self.damps) * (norm(ps) / self.chiN - 1))
            print("sigma: %.2f" % sigma)
            # Update A and Ainv with search path
            if self.counteval - self.eigeneval > self.update_crit:  # to achieve O(N ^ 2) do decomposition less frequently
                self.eigeneval = self.counteval
                t1 = time.time()
                v = pc @ Ainv # (1, spacedimen) * (spacedimen, N) -> (1,N)
                normv = v @ v.T
                # Directly update the A Ainv instead of C itself
                A = sqrt(1 - c1) * A + sqrt(1 - c1) / normv * (
                            sqrt(1 + normv * c1 / (1 - c1)) - 1) * v.T @ pc  # FIXME, dimension error
                Ainv = 1 / sqrt(1 - c1) * Ainv - 1 / sqrt(1 - c1) / normv * (
                            1 - 1 / sqrt(1 + normv * c1 / (1 - c1))) * Ainv @ v.T @ v
                t2 = time.time()
                print("A, Ainv update! Time cost: %.2f s" % (t2 - t1))
        # Generate new sample by sampling from Gaussian distribution
        new_samples = zeros((self.lambda_, N))
        self.randz = randn(self.lambda_, N)  # save the random number for generating the code.
        new_samples = self.xmean + sigma * self.randz @ A
        self.counteval += self.lambda_
        # Clever way to generate multivariate gaussian!!
        # Stretch the guassian hyperspher with D and transform the
        # ellipsoid by B mat linear transform between coordinates
        self.sigma, self.A, self.Ainv, self.ps, self.pc = sigma, A, Ainv, ps, pc,
        self._istep += 1
        return new_samples


class concat_wrapper:
    """Use two optimizers to work on two subspaces separately.
        optim1, optim2: any optimizer in this format
    """
    def __init__(self, optim1, optim2):
        self.optim1 = optim1
        self.optim2 = optim2
        self.sep = self.optim1.space_dimen

    def step_simple(self, scores, codes):
        new_codes1 = self.optim1.step_simple(scores, codes[:,:self.sep])
        new_codes2 = self.optim2.step_simple(scores, codes[:,self.sep:])
        return np.concatenate((new_codes1, new_codes2), axis=1)


class fix_param_wrapper:
    """Fix part of parameter and optimize the other part,
        optim: any optimizer in this format
        pre: True means fix the initial part of code,
             False means last part
    """
    def __init__(self, optim, fixed_code, pre=True):
        self.optim = optim
        self.fix_code = fixed_code
        self.pre = pre  # if the fix code in in the first part
        self.sep = fixed_code.shape[1]

    def step_simple(self, scores, codes):
        if self.pre:
            new_codes1 = self.optim.step_simple(scores, codes[:, self.sep:])
            return np.concatenate((np.repeat(self.fix_code, new_codes1.shape[0], axis=0), new_codes1), axis=1)
        else:
            new_codes1 = self.optim.step_simple(scores, codes[:, :-self.sep])
            return np.concatenate((new_codes1, np.repeat(self.fix_code, new_codes1.shape[0], axis=0)), axis=1)


def label2optimizer(methodlabel, init_code, GAN="BigGAN", Hdata=None):
    """ Registry of pre-defined optimizers.
        Input a label output an grad-free optimizer
    """
    # TODO: add default init_code
    if Hdata is None and "Hess" in methodlabel:
        # warnings for Hdata missing
        raise FileNotFoundError("Hessian not found! unable to use the Hessian based optimizers.")

    if GAN == "BigGAN":
        if methodlabel == "CholCMA":
            optim_cust = CholeskyCMAES(space_dimen=256, init_code=init_code, init_sigma=0.2,)
        elif methodlabel == "CholCMA_class":
            optim = CholeskyCMAES(space_dimen=128, init_code=init_code[:, 128:], init_sigma=0.06,)
            optim_cust = fix_param_wrapper(optim, init_code[:, :128], pre=True)
        elif methodlabel == "CholCMA_noise":
            optim = CholeskyCMAES(space_dimen=128, init_code=init_code[:, :128], init_sigma=0.3,)
            optim_cust = fix_param_wrapper(optim, init_code[:, 128:], pre=False)
        elif methodlabel == "CholCMA_prod":
            optim1 = CholeskyCMAES(space_dimen=128, init_code=init_code[:, :128], init_sigma=0.1,)
            optim2 = CholeskyCMAES(space_dimen=128, init_code=init_code[:, 128:], init_sigma=0.06,)
            optim_cust = concat_wrapper(optim1, optim2)
        elif methodlabel == "CholCMA_noA":
            optim_cust = CholeskyCMAES(space_dimen=256, init_code=init_code, init_sigma=0.2, Aupdate_freq=102)
        elif methodlabel == "HessCMA":
            eva = Hdata['eigvals_avg'][::-1]
            evc = Hdata['eigvects_avg'][:, ::-1]
            optim_cust = HessCMAES(space_dimen=256, init_code=init_code, init_sigma=0.2, )
            optim_cust.set_Hessian(eigvals=eva, eigvects=evc, expon=1 / 2.5)
        elif methodlabel == "HessCMA_noA":
            eva = Hdata['eigvals_avg'][::-1]
            evc = Hdata['eigvects_avg'][:, ::-1]
            optim_cust = HessCMAES(space_dimen=256, init_code=init_code, init_sigma=0.2, Aupdate_freq=102)
            optim_cust.set_Hessian(eigvals=eva, eigvects=evc, expon=1 / 2.5)
        elif methodlabel == "HessCMA_class":
            eva = Hdata['eigvals_clas_avg'][::-1]
            evc = Hdata['eigvects_clas_avg'][:, ::-1]
            optim_hess = HessCMAES(space_dimen=128, init_code=init_code[:, 128:], init_sigma=0.2, )
            optim_hess.set_Hessian(eigvals=eva, eigvects=evc, expon=1 / 2.5)
            optim_cust = fix_param_wrapper(optim_hess, init_code[:, :128], pre=True)
    elif GAN == "fc6":
        if methodlabel == "CholCMA":
            optim_cust = CholeskyCMAES(space_dimen=4096, init_code=init_code, init_sigma=3,)
        elif methodlabel == "CholCMA_noA":
            optim_cust = CholeskyCMAES(space_dimen=4096, init_code=init_code, init_sigma=3, Aupdate_freq=102)
        elif methodlabel == "HessCMA800":
            eva = Hdata['eigv_avg'][::-1]
            evc = Hdata['eigvect_avg'][:, ::-1]
            optim_cust = HessCMAES(space_dimen=4096, cutoff=800, init_code=init_code, init_sigma=0.8, )
            optim_cust.set_Hessian(eigvals=eva, eigvects=evc, cutoff=800, expon=1 / 5)
        elif methodlabel == "HessCMA500":
            eva = Hdata['eigv_avg'][::-1]
            evc = Hdata['eigvect_avg'][:, ::-1]
            optim_cust = HessCMAES(space_dimen=4096, cutoff=500, init_code=init_code, init_sigma=0.8, )
            optim_cust.set_Hessian(eigvals=eva, eigvects=evc, cutoff=500, expon=1 / 5)
        elif methodlabel == "HessCMA500_1":
            eva = Hdata['eigv_avg'][::-1]
            evc = Hdata['eigvect_avg'][:, ::-1]
            optim_cust = HessCMAES(space_dimen=4096, cutoff=500, init_code=init_code, init_sigma=0.4, )
            optim_cust.set_Hessian(eigvals=eva, eigvects=evc, cutoff=500, expon=1 / 4)
    return optim_cust

