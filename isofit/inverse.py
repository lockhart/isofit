#! /usr/bin/env python3
#
#  Copyright 2018 California Institute of Technology
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
# ISOFIT: Imaging Spectrometer Optimal FITting
# Author: David R Thompson, david.r.thompson@jpl.nasa.gov
#

import sys
import scipy as s
from common import spectrumLoad, chol_inv, eps
import scipy.optimize
from scipy.linalg import inv, norm, sqrtm, det, cholesky, qr
from hashlib import md5


debug = False
error_code = -1
check = md5()  # md5.new()


class Inversion:

    def __init__(self, config, forward):
        """Initialization specifies retrieval subwindows for calculating
        measurement cost distributions"""
        self.fm = forward
        self.wl = self.fm.wl
        self.windows = config['windows']
        self.winidx = s.array((), dtype=int)  # indices of retrieval windows
        inds = range(len(self.wl))
        for lo, hi in self.windows:
            idx = s.where(s.logical_and(self.wl > lo, self.wl < hi))[0]
            self.winidx = s.concatenate((self.winidx, idx), axis=0)
        self.counts = 0
        self.inversions = 0

    def calc_prior(self, x, geom):
        """Calculate prior distribution of radiance.  This depends on the 
        location in the state space.  Return the inverse covariance and 
        its square root (for non-quadratic error residual calculation)"""

        xa = self.fm.xa(x, geom)
        Sa = self.fm.Sa(x, geom)
        Sa_inv = s.real(chol_inv(Sa))
        Sa_inv_sqrt = s.real(sqrtm(Sa_inv))
        return xa, Sa, Sa_inv, Sa_inv_sqrt

    def calc_posterior(self, x, geom, rdn_meas):
        """Calculate posterior distribution of state vector. This depends 
        both on the location in the state space and the radiance (via noise)."""

        xa = self.fm.xa(x, geom)
        Sa = self.fm.Sa(x, geom)
        Sa_inv = chol_inv(Sa)
        K = self.fm.K(x, geom)
        Seps = self.fm.Seps(rdn_meas, geom, init=x)
        Seps_inv = chol_inv(Seps)
        S_hat = chol_inv(K.T.dot(Seps_inv).dot(K) + Sa_inv)
        G = S_hat.dot(K.T).dot(Seps_inv)
        return S_hat, K, G

    def calc_Seps(self, rdn_meas, geom, init=None):
        """Calculate (zero-mean) measurement distribution in radiance terms.  
        This depends on the location in the state space. This distribution is 
        calculated over one or more subwindows of the spectrum. Return the 
        inverse covariance and its square root"""

        Seps = self.fm.Seps(rdn_meas, geom, init=init)
        Seps = s.array([Seps[i, self.winidx] for i in self.winidx])
        Seps_inv = s.real(chol_inv(Seps))
        Seps_inv_sqrt = s.real(sqrtm(Seps_inv))
        return Seps_inv, Seps_inv_sqrt

    def invert(self, rdn_meas, geom, out=None, init=None):
        """Inverts a meaurement and returns a state vector.

           Parameters:

             rdn_meas       - a one-D scipy vector of radiance in uW/nm/sr/cm2
             geom           - a geometry object.
             plot_directory - the base directory to which diagnostics are
                                writtena preinitialized ForwardModel object

           Returns a tuple consisting of the following:

             lrfl           - the converged lambertian surface reflectance
             path           - the converged path radiance estimate
             mdl            - the modeled radiance estimate
             S_hat          - the posterior covariance of the state vector
             G              - the G matrix from the CD Rodgers 2000 formalism
             xopt           - the converged state vector solution"""

        """the least squares library seems to crash if we call it too many
    times without reloading.  Memory leak?"""
        # reload(scipy.optimize)

        """Calculate the initial solution, if needed."""
        if init is None:
            init = self.fm.init(rdn_meas, geom)

        """Seps is the covariance of "observation noise" including both 
    measurement noise from the instrument as well as variability due to 
    unknown variables.  For speed, we will calculate it just once based
    on the initial solution (a potential minor source of inaccuracy)"""
        Seps_inv, Seps_inv_sqrt = self.calc_Seps(rdn_meas, geom, init=init)

        def jac(x):
            """Calculate measurement jacobian and prior jacobians with 
            respect to COST function.  This is the derivative of cost with
            respect to the state.  The Cost is expressed as a vector of 
            'residuals' with respect to the prior and measurement, 
            expressed in absolute terms (not quadratic) for the solver, 
            It is the square root of the Rodgers et. al Chi square version.
            All measurement distributions are calculated over subwindows 
            of the full spectrum."""

            if debug:
                check.update(x)
                print("JAC #1: ", check.hexdigest())

            # jacobian of measurment cost term WRT state vector.
            K = self.fm.K(x, geom)[self.winidx, :]
            meas_jac = Seps_inv_sqrt.dot(K)

            # jacobian of prior cost term with respect to state vector.
            xa, Sa, Sa_inv, Sa_inv_sqrt = self.calc_prior(x, geom)
            prior_jac = Sa_inv_sqrt

            # The total cost vector (as presented to the solver) is the
            # concatenation of the "residuals" due to the measurement
            # and prior distributions. They will be squared internally by
            # the solver.
            total_jac = s.concatenate((meas_jac, prior_jac), axis=0)

            if debug:
                check.update(s.real(total_jac))
                print("JAC #2: ", check.hexdigest())

            return s.real(total_jac)

        def err(x):
            """Calculate cost function expressed here in absolute terms
            (not quadratic) for the solver, i.e. the square root of the 
            Rodgers et. al Chi square version.  We concatenate 'residuals'
            due to measurment and prior terms, suitably scaled.
            All measurement distributions are calculated over subwindows 
            of the full spectrum."""

            if debug:
                check.update(x)
                print("ERR #1: ", check.hexdigest())

            # Measurement cost term.  Will calculate reflectance and Ls from
            # the state vector.
            est_rdn = self.fm.calc_rdn(x, geom, rfl=None, Ls=None)
            est_rdn_window = est_rdn[self.winidx]
            meas_window = rdn_meas[self.winidx]
            meas_resid = (est_rdn_window-meas_window).dot(Seps_inv_sqrt)

            # Prior cost term
            xa, Sa, Sa_inv, Sa_inv_sqrt = self.calc_prior(x, geom)
            prior_resid = (x - xa).dot(Sa_inv_sqrt)

            # total cost
            total_resid = s.concatenate((meas_resid, prior_resid))

            # diagnostic
            self.counts = self.counts + 1
            sys.stdout.write('Iteration: '+str(self.counts))
            sys.stdout.write(' Residual: %f\n' % sum(pow(total_resid, 2)))
            sys.stdout.write(' '+self.fm.summarize(x, geom)+'\n')

            # Plot interim solution?
            if out is not None:
                out.plot_spectrum(x, rdn_meas, geom)

            if debug:
                check.update(s.real(total_resid))
                print("ERR #2: ", check.hexdigest())

            return s.real(total_resid)

        # Initialize and invert
        x0 = init.copy()
        xopt = scipy.optimize.least_squares(err, x0, jac=jac, method='trf',
                                            bounds=(
                                                self.fm.bounds[0]+eps, self.fm.bounds[1]-eps),
                                            xtol=1e-4, ftol=1e-4, gtol=1e-4, x_scale=self.fm.scale,
                                            max_nfev=20, tr_solver='exact')
        return xopt.x

    def forward_uncertainty(self, x, rdn_meas, geom):
        """Converged estimates of path radiance, radiance, reflectance
        # Also calculate the posterior distribution and Rodgers K, G matrices"""

        dark_surface = s.zeros(rdn_meas.shape)
        path = self.fm.calc_rdn(x, geom, rfl=dark_surface)
        mdl = self.fm.calc_rdn(x, geom, rfl=None, Ls=None)
        lrfl = self.fm.calc_lrfl(x, geom)
        S_hat, K, G = self.calc_posterior(x, geom, rdn_meas)
        return lrfl, mdl, path, S_hat, K, G

    def invert_algebraic(self, rdn_meas, x, geom):
        x0 = self.fm.init(rdn_meas, geom)
        xopt, coeffs = self.fm.invert_algebraic(x, rdn_meas, geom)
        return x0, xopt, coeffs


if __name__ == '__main__':
    main()
