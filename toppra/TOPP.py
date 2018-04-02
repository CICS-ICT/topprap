"""The TOPP-RA package currently is implemented using `qpOASES`.

Interfaces to other solvers will be added in the future.
"""
import numpy as np
from qpoases import (PyOptions as Options, PyPrintLevel as PrintLevel,
                     PyReturnValue as ReturnValue, PySQProblem as SQProblem)
import logging
import quadprog

logger = logging.getLogger(__name__)
SUCCESSFUL_RETURN = ReturnValue.SUCCESSFUL_RETURN


qpOASESReturnValueDict = {
    0: "SUCCESSFUL_RETURN",
    61: "HOTSTART_STOPPED_INFEASIBILITY",
    33: "INIT_FAILED",
    35: "INIT_FAILED_CHOLESKY",
    38: "INIT_FAILED_UNBOUNDEDNESS",
    34: "INIT_FAILED_TQ",
    36: "INIT_FAILED_HOTSTART",
    37: "INIT_FAILED_INFEASIBILITY "
}

# Constants
SUPERTINY = 1e-10
TINY = 1e-8
SMALL = 1e-5
INFTY = 1e8
MAXU = 100  # Max limit for `u`
MAXX = 100  # Max limit for `x`

###############################################################################
#                   PathParameterization Algorithms/Objects                   #
###############################################################################


class qpOASESPPSolver(object):
    """An implementation of TOPP-RA using the QP solver ``qpOASES``.

    Parameters
    ----------
    constraint_set : list
        A list of  :class:`.PathConstraint`
    verbose : bool
        More verbose output.

    Attributes
    ----------
    Ks : array, shape (N+1, 2)
        Controllable sets/intervals.
    I0 : array
    IN : array
    ss : array
        Discretized path positions.
    nm : int
        Dimension of the canonical constraint part.
    nv : int
        Dimension of the combined slack.
    niq : int
        Dimension of the inequalities on slack.
    neq : int
        Dimension of the equalities on slack.
    nV : int
        Dimension of the optimization variable.
    nC : int
        (``qpOASES``) Number of constraints .
    A : array
        (``qpOASES``) Shape (N+1, nC, nV).
    lA : array
        (``qpOASES``) Shape (N+1, nC).
    hA : array
        (``qpOASES``) Shape (N+1, nC).
    l : array
        (``qpOASES``) Shape (N+1, nV).
    h : array
        (``qpOASES``) Shape (N+1, nV).
    H : array
        (``qpOASES``) Shape (nV, nV).
    g : array
        (``qpOASES``) Shape (nV,).

    Notes
    -----

    Attributes tagged with (``qpOASES``) are internal variables used with
    the ``qpOASES`` solver. For details on their construction, see belows.

    The first ``nop`` rows of ``A`` are operational rows. They are
    reserved to specify additional constraints. For example, to specify the constraints
    that the next stage state must lie inside the target set.

    1. the controllable sets; TODO
    2. the reachable sets; TODO

    This class uses ``qpOASES`` to solve Quadratic Programs of the form

    .. math::
            min  \quad   & 0.5 (u, x, v^T) \mathbf{H} (u, x, v^T)^T + \mathbf{g}^T (u, x, v^T)^T \\\\
            s.t. \quad   & \mathbf{l_A}[i] \leq \mathbf{A}[i] (u, x, v^T)^T \leq \mathbf{h_A}[i] \\\\
                         & \mathbf{l}[i]  \leq   (u, x, v^T)^T \leq \mathbf{h}[i]

    The matrices :math:`A[i], l_A[i], h_A[i]` consist of
    four row sections. The four sections are:

    1. operational: see above;
    2. canonical;
    3. non-canonical;
    4. non-canonical hard-bound.

    For more details on the last three sections, see
    :class:`.PathConstraint`'s docstring.

    The vectors :math:`l[i], h[i]` contain hard-bounds on :math:`u, x,
    \mathbf{v}` respectively.

    """
    def __init__(self, constraint_set, verbose=False):
        self.I0 = np.r_[0, 1e-4]  # Start and end velocity interval
        self.IN = np.r_[0, 1e-4]
        self.ss = constraint_set[0].ss
        self.Ds = self.ss[1:] - self.ss[:-1]
        self.N = constraint_set[0].N
        for c in constraint_set:
            assert np.allclose(c.ss, self.ss)

        # Controllable subsets
        self._K = - np.ones((self.N + 1, 2))
        self._L = - np.ones((self.N + 1, 2))
        self.nop = 3  # Operational row, used for special constraints

        self.constraint_set = constraint_set

        # Pre-processing: Compute shape and init zero coeff matrices
        self._init_matrices(constraint_set)
        self._fill_matrices()
        summary_msg = """
Initialize Path Parameterization instance
------------------------------
\t N                  : {:8d}
\t No. of constraints : {:8d}
\t No. of slack var   : {:8d}
\t No. of can. ineq.  : {:8d}
\t No. of equalities  : {:8d}
\t No. of inequalities: {:8d}
""".format(self.N, len(self.constraint_set),
           self.nv, self. nm, self.neq, self.niq)
        logger.info(summary_msg)

        # Setup solvers
        self._init_qpoaes_solvers(verbose)

    @property
    def K(self):
        """ The Controllable subsets.
        """
        controllable_subsets = self._K[:, 0] > - TINY
        return self._K[controllable_subsets]

    @property
    def L(self):
        """ The Reachable subsets.
        """
        reachable_subsets = self._L[:, 0] > - TINY
        return self._L[reachable_subsets]

    def set_start_interval(self, I0):
        """Set starting *squared* velocities interval.


        Parameters
        ----------
        I0: array, or float
            (2, 0) array, the interval of starting squared path velocities.
            Can also be a float.

        Raises
        ------
        AssertionError
            If `I0` is a single, negative float. Or if `I0[0] > I0[1]`.

        """
        I0 = np.r_[I0].astype(float)
        if I0.shape[0] == 1:
            I0 = np.array([I0[0], I0[0]])
        elif I0.shape[0] > 2:
            raise ValueError('Input I0 has wrong dimension: {}'.format(I0.shape))
        assert I0[1] >= I0[0], "Illegal input: non-increase end-points."
        assert I0[0] >= 0, "Illegal input: negative lower end-point."

        self.I0 = I0

    def set_goal_interval(self, IN):
        """Set the goal squared velocity interval.

        Parameters
        ----------
        IN: array or float
            A single float, or a (2, ) array setting the goal
            `(x_lower, x_higher)` squared path velocities.

        Raises
        ------
        AssertionError
            If `IN` is a single, negative float. Or if `IN[0] > IN[1]`.

        """
        IN = np.r_[IN].astype(float)
        if IN.shape[0] == 1:
            IN = np.array([IN[0], IN[0]])
        elif IN.shape[0] > 2:
            raise ValueError('Input IN has wrong dimension: {}'.format(IN.shape))
        assert IN[1] >= IN[0], "Illegal input: non-increase end-points."
        assert IN[0] >= 0, "Illegal input: negative lower end-point."

        self.IN = IN

    def _init_qpoaes_solvers(self, verbose):
        """Initialize two `qpOASES` solvers.

        One solver is used for solving the upper bounds. The other
        solver is used for solving the lower bound.

        Parameters
        ----------
        verbose: bool
            Set verbose output for the `qpOASES` solvers.
        """
        # `nWSR` stands for number of Working Set Recalculation. When
        # solving problems with `qpOASES`, the maximum nWSR is to be
        # input to the algorithm. After solving finished, the variable
        # become the number of Working Set Recalculation carried out.
        self.nWSR_cnst = 1000
        _, nC, nV = self.A.shape
        # Setup solver
        options = Options()
        if verbose:
            logger.debug("Set qpOASES print level to HIGH")
            options.printLevel = PrintLevel.HIGH
        else:
            logger.debug("Set qpOASES print level to NONE")
            options.printLevel = PrintLevel.NONE
        self.solver_up = SQProblem(nV, nC)
        self.solver_up.setOptions(options)
        self.solver_down = SQProblem(nV, nC)
        self.solver_down.setOptions(options)

    def _init_matrices(self, constraint_set):
        """Initialize coefficient matrices.

        See matrices marked with (`qpOASES`) in the class docstring.

        Parameters
        ----------
        constraint_set : list
            A list of  :class:`.PathConstraint`

        """
        self.nm = sum([c.nm for c in constraint_set])
        self.niq = sum([c.niq for c in constraint_set])
        self.neq = sum([c.neq for c in constraint_set])
        self.nv = sum([c.nv for c in constraint_set])
        self.nV = self.nv + 2
        self.nC = self.nop + self.nm + self.neq + self.niq

        self.H = np.zeros((self.nV, self.nV))
        self.g = np.zeros(self.nV)
        # fixed bounds
        self.l = np.zeros((self.N + 1, self.nV))
        self.h = np.zeros((self.N + 1, self.nV))
        # lA, A, hA constraints
        self.lA = np.zeros((self.N + 1, self.nC))
        self.hA = np.zeros((self.N + 1, self.nC))
        self.A = np.zeros((self.N + 1, self.nC, self.nV))
        self._xfull = np.zeros(self.nV)  # interval vector, store primal
        self._yfull = np.zeros(self.nC)  # interval vector, store dual

    def _fill_matrices(self):
        """Fill coefficient matrices with input constraints.

        For more details, see the class docstring.

        """
        self.g.fill(0)
        self.H.fill(0)
        # A
        self.A.fill(0)
        self.A[:, :self.nop, :] = 0.  # operational rows
        self.lA[:, :self.nop] = 0.
        self.hA[:, :self.nop] = 0.
        # canonical
        row = self.nop
        for c in filter(lambda c: c.nm != 0, self.constraint_set):
            self.A[:, row: row + c.nm, 0] = c.a
            self.A[:, row: row + c.nm, 1] = c.b
            self.lA[:, row: row + c.nm] = - INFTY
            self.hA[:, row: row + c.nm] = - c.c
            row += c.nm

        # equalities
        row = self.nop + self.nm
        col = 2
        for c in filter(lambda c: c.neq != 0, self.constraint_set):
            self.A[:, row: row + c.neq, 0] = c.abar
            self.A[:, row: row + c.neq, 1] = c.bbar
            self.A[:, row: row + c.neq, col: col + c.nv] = - c.D
            self.lA[:, row: row + c.neq] = - c.cbar
            self.hA[:, row: row + c.neq] = - c.cbar
            row += c.neq
            col += c.nv

        # inequalities
        row = self.nop + self.nm + self.neq
        col = 2
        for c in filter(lambda c: c.niq != 0, self.constraint_set):
            self.A[:, row: row + c.niq, col: col + c.nv] = c.G
            self.lA[:, row: row + c.niq] = c.lG
            self.hA[:, row: row + c.niq] = c.hG
            row += c.niq
            col += c.nv

        # bounds on var
        self.l[:, 0] = - MAXU  # - infty <= u <= infty
        self.h[:, 0] = MAXU
        self.l[:, 1] = 0  # 0 <= x <= infty
        self.h[:, 1] = MAXX
        row = 2
        for c in filter(lambda c: c.nv != 0, self.constraint_set):
            self.l[:, row: row + c.nv] = c.l
            self.h[:, row: row + c.nv] = c.h
            row += c.nv

    def solve_controllable_sets(self, eps=1e-14):
        """Solve for controllable sets :math:`\mathcal{K}_i(I_{\mathrm{goal}})`.

        The i-th controllable set :math:`\mathcal{K}_i(I_{\mathrm{goal}})`
        is the set of states at stage :math:`i` such that there exists
        at least a sequence of admissible controls
        :math:`(u_i,\dots,u_{N-1})` that drives it to
        :math:`\mathcal{I}_{\mathrm{goal}}`.

        Notes
        -----
        The interval computed with `one_step` is **not** the true
        optimal solution. They differ by some small tolerance. This is
        due to numerical errors with the the QP solver
        ``qpOASES``. Thus it might happen that the end point of
        the interval is not actually controllable.

        Setting ``eps`` to non-zero slight restrict the controllable
        set, and thus address this problem.

        Parameters
        ----------
        eps: float, optional
             A small margin to guard againts accumulating numerical
             error while computing the one-step sets.

        Returns
        -------
        out: bool.
             True if :math:`\mathcal{K}_0(I_{\mathrm{goal}})` is not empty.

        """
        self.reset_operational_rows()
        self.nWSR_up = np.ones((self.N + 1, 1), dtype=int) * self.nWSR_cnst
        self.nWSR_down = np.ones((self.N + 1, 1), dtype=int) * self.nWSR_cnst
        xmin, xmax = self.proj_x_admissible(self.N, self.IN[0],
                                            self.IN[1], init=True)
        if xmin is None:
            logger.warn("Fail to project the interval IN to feasibility")
            return False
        else:
            self._K[self.N, 1] = xmax
            self._K[self.N, 0] = xmin

        init = True
        for i in range(self.N - 1, -1, -1):
            xmin_i, xmax_i = self.one_step(
                i, self._K[i + 1, 0], self._K[i + 1, 1], init=init)
            # Turn init off, use hotstart
            init = False
            if xmin_i is None:
                logger.warn("Find controllable set K(%d) fails!", i)
                return False
            else:
                self._K[i, 1] = xmax_i - eps  # Buffer for numerical error
                self._K[i, 0] = max(xmin_i, 0.0)  # Negative end-point not allowed.

        return True

    def solve_reachable_sets(self):
        """Solve for reachable sets :math:`\mathcal{L}_i(I_{init})`.

        Returns
        -------
        out: bool
             True if :math:`\mathcal{L}_0(I_{init})` is not empty.
             False otherwise.
        """
        self.reset_operational_rows()
        xmin, xmax = self.proj_x_admissible(
            0, self.I0[0], self.I0[1], init=True)
        if xmin is None:
            logger.warn("Fail to project the interval I0 to feasibility")
            return False
        else:
            self._L[0, 1] = xmax
            self._L[0, 0] = xmin
        for i in range(self.N):
            init = (True if i <= 1 else False)
            xmin_nx, xmax_nx = self.reach(i, self._L[i, 0], self._L[i, 1], init=init)
            if xmin_nx is None:
                logger.warn("Forward propagation from L%d failed ", i)
                return False
            xmin_pr, xmax_pr = self.proj_x_admissible(i + 1, xmin_nx, xmax_nx, init=init)
            if xmin_pr is None:
                logger.warn("Projection for L{:d} failed".format(i))
                return False
            else:
                self._L[i + 1, 1] = xmax_pr
                self._L[i + 1, 0] = xmin_pr
        return True

    def solve_topp(self, save_solutions=False, reg=0.):
        """Solve for the time-optimal path-parameterization

        Parameters
        ----------
        save_solutions : bool
            Save solutions of each step.
        reg : float
            Regularization gain.

        Returns
        -------
        us : array
            Shape (N,). Contains the TOPP's controls.
        xs : array
            Shape (N+1,). Contains the TOPP's squared velocities.
        """
        if save_solutions:
            self._xfulls = np.empty((self.N, self.nV))
            self._yfulls = np.empty((self.N, self.nC))
        # Backward pass
        controllable = self.solve_controllable_sets()
        # Check controllability
        infeasible = (self._K[0, 1] < self.I0[0] or self._K[0, 0] > self.I0[1])

        if not controllable or infeasible:
            msg = """
Unable to parameterizes this path:
- K(0) is empty : {0}
- sd_start not in K(0) : {1}
""".format(controllable, infeasible)
            raise ValueError(msg)

        # Forward pass
        xs = np.zeros(self.N + 1)
        us = np.zeros(self.N)
        xs[0] = min(self._K[0, 1], self.I0[1])
        _, _ = self.greedy_step(0, xs[0], self._K[1, 0], self._K[1, 1],
                                init=True, reg=reg)  # Warm start
        for i in range(self.N):
            u_, x_ = self.greedy_step(i, xs[i], self._K[i + 1, 0], self._K[i + 1, 1],
                                      init=False, reg=reg)  # Hot start
            xs[i + 1] = x_
            us[i] = u_
            if save_solutions:
                self._xfulls[i] = self._xfull.copy()
        return us, xs

    @property
    def slack_vars(self):
        """ Recent stored slack variable.
        """
        return self._xfulls[:, 2:]

    def reset_operational_rows(self):
        """Zero operational rows.

        It is important to use this function whenever the next
        operation is of different *kind*.

        In fact this routine is very cheap, thus when in doubt always
        use it.

        Example:

        >>> solver.one_step(0, 1)
        >>> solver.reset_operational_rows()
        >>> solver.reach(0, 1)
        """
        # reset all rows
        self.A[:, :self.nop] = 0
        self.lA[:, :self.nop] = 0
        self.hA[:, :self.nop] = 0
        self.H[:, :] = 0
        self.g[:] = 0

    ###########################################################################
    #                    Main Set Projection Functions                        #
    ###########################################################################
    def one_step(self, i, xmin, xmax, init=False):
        """Compute the one-step set :math:`\mathcal{Q}_i` for the interval (`xmin`, `xmax`).

        If the projection is not feasible (for example when `xmin` >
        `xmax`), then return (`None`, `None`).

        Note
        -----
        The variables `self.nWSR_up` and `self.nWSR_down` need to be
        initialized prior to using this function. See
        :func:`qpOASESPPSolver.solve_controllable_sets` for more details.

        Parameters
        ----------
        i : int
            Index to compute the controllable set.
        xmin : float
            Maximum target state.
        xmax : float
            Minimum target state.
        init : bool, optional
            If `True`, coldstart. Else, hotstart.

        Returns
        -------
        xmin_i : float
            Lower end-point of :math:`\mathcal{Q}_i`.
        xmax_i : float
            Higher end-point of :math:`\mathcal{Q}_i`.
        """
        # Set constraint: xmin <= 2 ds u + x <= xmax
        self.reset_operational_rows()
        nWSR_up = np.array([self.nWSR_cnst])
        nWSR_down = np.array([self.nWSR_cnst])
        self.A[i, 0, 1] = 1
        self.A[i, 0, 0] = 2 * (self.ss[i + 1] - self.ss[i])
        self.lA[i, 0] = xmin
        self.hA[i, 0] = xmax

        if init:
            # upper solver solves for max x
            self.g[1] = -1.
            res_up = self.solver_up.init(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_up)

            # lower solver solves for min x
            self.g[1] = 1.
            res_down = self.solver_down.init(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_down)
        else:
            # upper solver solves for max x
            self.g[1] = -1.
            res_up = self.solver_up.hotstart(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_up)

            # lower bound
            self.g[1] = 1.
            res_down = self.solver_down.hotstart(
                self.H, self.g, self.A[i], self.l[i], self.h[i],
                self.lA[i], self.hA[i], nWSR_down)

        # Check result
        if (res_up != SUCCESSFUL_RETURN) or (res_down != SUCCESSFUL_RETURN):
            logger.warn("""
Computing one-step failed.

    INFO:
    ----
        i                     = {}
        xmin                  = {}
        xmax                  = {}
        warm_start            = {}
        upper LP solve status = {}
        lower LP solve status = {}
""".format(i, xmin, xmax, init, res_up, res_down))

            return None, None

        # extract solution
        self.solver_up.getPrimalSolution(self._xfull)
        xmax_i = self._xfull[1]
        self.solver_down.getPrimalSolution(self._xfull)
        xmin_i = self._xfull[1]
        return xmin_i, xmax_i

    def reach(self, i, xmin, xmax, init=False):
        """Compute the reach set from [xmin, xmax] at stage i.

        If the projection is not feasible (for example when xmin >
        xmax), then return None, None.

        Parameters
        ----------
        i : int
        xmin : float
        xmax: float
        init: bool, optional
            Use qpOASES with hotstart.

        Returns
        -------
        xmin_i: float
        xmax_i: float
        """

        self.A[i, 0, 1] = 1
        self.A[i, 0, 0] = 0.
        self.lA[i, 0] = xmin
        self.hA[i, 0] = xmax

        # upper bound
        nWSR_up = np.array([self.nWSR_cnst])
        self.g[0] = -2. * (self.ss[i + 1] - self.ss[i])
        self.g[1] = -1.
        if init:
            res_up = self.solver_up.init(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_up)
        else:
            res_up = self.solver_up.hotstart(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_up)

        nWSR_down = np.array([self.nWSR_cnst])
        self.g[0] = 2. * (self.ss[i + 1] - self.ss[i])
        self.g[1] = 1.
        if init:
            res_down = self.solver_down.init(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_down)
        else:
            res_down = self.solver_down.hotstart(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_down)

        if (res_up != SUCCESSFUL_RETURN) or (res_down != SUCCESSFUL_RETURN):
            logger.warn("""
Computing reach set failed.

    INFO:
    ----
        i                     = {}
        xmin                  = {}
        xmax                  = {}
        warm_start            = {}
        upper LP solve status = {}
        lower LP solve status = {}
""".format(i, xmin, xmax, init, res_up, res_down))
            return None, None

        # extract solution
        xmax_i = -self.solver_up.getObjVal()
        xmin_i = self.solver_down.getObjVal()
        return xmin_i, xmax_i

    def proj_x_admissible(self, i, xmin, xmax, init=False):
        """Project the interval [xmin, xmax] back to the feasible set.

        If the projection is infeasible, for example when xmin > xmax
        or when the infeasible set if empty, then return None, None.

        Parameters
        ----------
        i: int
           Index of the path position where the project is at.
        xmin: float
            Lower bound of the interval to be projected
        xmax: float
            Upper bound of the interval to be projected
        init: bool, optional
            If True, use qpOASES without hotstart.
            If False, use hotstart.

        Returns
        -------
        xmin_i: float
        xmax_i: float

        Note
        -----

        If one find unreasonable results such as (0, 0) constantly
        appear, then it is a good idea to reset the internal
        operational matrices using :func:`qpOASESPPSolver.reset_operational_rows`.


        """

        self.A[i, 0, 1] = 1
        self.A[i, 0, 0] = 0.
        self.lA[i, 0] = xmin
        self.hA[i, 0] = xmax

        # upper bound
        nWSR_up = np.array([self.nWSR_cnst])
        self.g[0] = 0.
        self.g[1] = -1.
        if init:
            res_up = self.solver_up.init(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_up)
        else:
            res_up = self.solver_up.hotstart(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_up)

        nWSR_down = np.array([self.nWSR_cnst])
        self.g[0] = 0.
        self.g[1] = 1.
        if init:
            res_down = self.solver_down.init(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_down)
        else:
            res_down = self.solver_down.hotstart(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_down)

        if (res_up != SUCCESSFUL_RETURN) or (res_down != SUCCESSFUL_RETURN):
            logger.warn("""
Computing projection failed.

    INFO:
    ----
        i                     = {}
        xmin                  = {}
        xmax                  = {}
        warm_start            = {}
        upper LP solve status = {}
        lower LP solve status = {}
""".format(i, xmin, xmax, init, res_up, res_down))
            logger.warn(
                "qpOASES error code {:d} is {}".format(
                    res_up,
                    qpOASESReturnValueDict[res_up]))
            return None, None

        # extract solution
        self.solver_up.getPrimalSolution(self._xfull)
        xmax_i = self._xfull[1]
        self.solver_down.getPrimalSolution(self._xfull)
        xmin_i = self._xfull[1]
        assert xmin_i <= xmax_i + SUPERTINY, "i:= {:d}, xmin:= {:f}, xmax:={:f}".format(i, xmin_i, xmax_i)
        if xmin_i > xmax_i:  # a problematic condition when xmin_i essentially equals xmax_i
            xmax_i = xmin_i
        return xmin_i, xmax_i

    def greedy_step(self, i, x, xmin, xmax, init=False, reg=0.):
        """ Take a forward greedy step from position s[i], state x.

        If the projection is infeasible (for example when `xmin` >
        `xmax`), then `(None, None)` is returned.

        If the function terminates successfully, `x_greedy` is
        guaranteed to be positive.

        Parameters
        ----------
        i: int
        x: float
        x_min: float
        x_max: float
        init: bool
        reg: float

        Returns
        -------
        u_greedy: float
            If infeasible, returns None.
        x_greedy: float
            If infeasible, returns None.

        """
        self.reset_operational_rows()

        # Enforce x == xs[i]
        self.A[i, 0, 1] = 1.
        self.A[i, 0, 0] = 0.
        self.lA[i, 0] = x
        self.hA[i, 0] = x
        # Constraint 2: xmin <= 2 ds u + x <= xmax
        self.lA[i, 1] = xmin
        self.hA[i, 1] = xmax
        self.A[i, 1, 1] = 1.
        self.A[i, 1, 0] = 2 * self.Ds[i]

        nWSR_topp = np.array([self.nWSR_cnst])  # The number of "constraint flipping"
        # Objective
        # max  u + reg ||v||_2^2
        self.g[0] = -1.
        if self.nv != 0:
            self.H[2:, 2:] += np.eye(self.nv) * reg

        if init:
            res_up = self.solver_up.init(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_topp)
        else:
            res_up = self.solver_up.hotstart(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_topp)

        if (res_up != SUCCESSFUL_RETURN):
            logger.warn("Non-optimal solution at i=%d. Returning default.", i)
            return None, None

        # extract solution
        self.solver_up.getPrimalSolution(self._xfull)
        # self.solver_up.getDualSolution(self._yfull)  # cause failure
        u_greedy = self._xfull[0]
        x_greedy = x + 2 * self.Ds[i] * u_greedy
        assert x_greedy + SUPERTINY >= 0, "Negative state (forward pass):={:f}".format(x_greedy)
        if x_greedy < 0:
            x_greedy = x_greedy + SUPERTINY
        return u_greedy, x_greedy

    def least_greedy_step(self, i, x, xmin, xmax, init=False, reg=0.):
        """Find min u such that xmin <= x + 2 ds u <= xmax.

        If the projection is infeasible (for example when `xmin` >
        `xmax`), then `(None, None)` is returned.

        If the function terminates successfully, `x_least_greedy` is
        guaranteed to be positive.

        Parameters
        ----------
        i: int
        x: float
        x_min: float
        x_max: float
        init: bool
        reg: float

        Returns
        -------
        u_greedy: float
            If infeasible, returns None.
        x_greedy: float
            If infeasible, returns None.

        """
        # Setup
        self.reset_operational_rows()
        nWSR_max = int(self.nWSR_cnst)
        # Constraint 1: x = x
        self.A[i, 0] = [0, 1]
        self.lA[i, 0] = x
        self.hA[i, 0] = x
        # Constraint 2: xmin <= 2 ds u + x <= xmax
        self.A[i, 1] = [2 * self.Ds[i], 1]
        self.lA[i, 1] = xmin
        self.hA[i, 1] = xmax

        # Objective
        # max  u + reg ||v||_2^2
        self.g[0] = 1.
        if self.nv != 0:
            self.H[2:, 2:] += np.eye(self.nv) * reg

        if init:
            res_up = self.solver_up.init(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_max)
        else:
            res_up = self.solver_up.hotstart(
                self.H, self.g, self.A[i], self.l[i], self.h[i], self.lA[i],
                self.hA[i], nWSR_max)

        if (res_up != SUCCESSFUL_RETURN):
            logger.warn("Non-optimal solution at i=%d. Returning (None, None).", i)
            return None, None

        # extract solution
        self.solver_up.getPrimalSolution(self._xfull)
        # self.solver_up.getDualSolution(self._yfull)  # cause failure
        u_least_greedy = self._xfull[0]
        x_least_greedy = x + 2 * self.Ds[i] * u_least_greedy
        assert x_least_greedy + SUPERTINY >= 0, "Negative state (forward pass):={:f}".format(x_least_greedy)
        if x_least_greedy < 0:
            x_least_greedy = x_least_greedy + SUPERTINY
        return u_least_greedy, x_least_greedy
