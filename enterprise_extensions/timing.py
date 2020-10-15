# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals
import os, glob, copy, ephem, math
import numpy as np
from collections import defaultdict, OrderedDict
from enterprise.signals import parameter
from enterprise.signals import signal_base, gp_signals
from enterprise.signals import deterministic_signals
from scipy.stats import truncnorm


def BoundNormPrior(value, mu=0, sigma=1, pmin=-1, pmax=1):
    """Prior function for InvGamma parameters."""
    low, up = (pmin - mu) / sigma, (pmax - mu) / sigma
    return truncnorm.pdf(value, loc=mu, scale=sigma, a=low, b=up)


def BoundNormSampler(mu=0, sigma=1, pmin=-1, pmax=1, size=None):
    """Sampling function for Uniform parameters."""
    low, up = (pmin - mu) / sigma, (pmax - mu) / sigma
    return truncnorm.rvs(loc=mu, scale=sigma, a=low, b=up, size=size)


def BoundedNormal(mu=0, sigma=1, pmin=-1, pmax=1, size=None):
    """Class factory for bounded Normal parameters."""

    class BoundedNormal(parameter.Parameter):
        _prior = parameter.Function(
            BoundNormPrior, mu=mu, sigma=sigma, pmin=pmin, pmax=pmax
        )
        _sampler = staticmethod(BoundNormSampler)
        _size = size
        _mu = mu
        _sigma = sigma
        _pmin = pmin
        _pmax = pmax

        def __repr__(self):
            return "{}: BoundedNormal({},{}, [{},{}])".format(
                self.name, mu, sigma, pmin, pmax
            ) + ("" if self._size is None else "[{}]".format(self._size))

    return BoundedNormal


def get_default_physical_tm_priors():
    """
    Fills dictionary with physical bounds on timing parameters
    """
    physical_tm_priors = {}
    physical_tm_priors["E"] = {"pmin": 0.0, "pmax": 0.9999}
    physical_tm_priors["ECC"] = {"pmin": 0.0, "pmax": 0.9999}
    physical_tm_priors["SINI"] = {"pmin": 0.0, "pmax": 1.0}
    physical_tm_priors["PX"] = {"pmin": 0.0}
    physical_tm_priors["M2"] = {"pmin": 1e-10}

    return physical_tm_priors


def get_pardict(psrs, datareleases):
    """assigns a parameter dictionary for each psr per dataset the parfile values/errors
    :psrs:
        objs, enterprise pulsar instances corresponding to datareleases
    :datareleases:
        list, list of datareleases
    """
    pardict = {}
    for psr, dataset in zip(psrs, datareleases):
        pardict[psr.name] = {}
        pardict[psr.name][dataset] = {}
        for par, vals, errs in zip(
            psr.fitpars[1:], psr.t2pulsar.vals(), psr.t2pulsar.errs()
        ):
            pardict[psr.name][dataset][par] = {}
            pardict[psr.name][dataset][par]["val"] = vals
            pardict[psr.name][dataset][par]["err"] = errs
    return pardict


def get_astrometric_priors(astrometric_px_file="../parallaxes.json"):
    # astrometric_px_file = '../parallaxes.json'
    astrometric_px = {}
    with open(astrometric_px_file, "r") as pxf:
        astrometric_px = json.load(pxf)
        pxf.close()

    return astrometric_px


def get_prior(
    prior_type,
    prior_sigma,
    prior_lower_bound,
    prior_upper_bound,
    mu=0.0,
    num_params=None,
):
    """
    Returns the requested prior for a parameter
    :param prior_type: prior on timing parameters.
    :param prior_sigma: Sets the sigma on timing parameters for normal distribution draws
    :param prior_lower_bound: Sets the lower bound on timing parameters for bounded normal and uniform distribution draws
    :param prior_upper_bound: Sets the upper bound on timing parameters for bounded normal and uniform distribution draws
    :param num_params: number of timing parameters assigned to prior. Default is None (ie. only one)
    """
    if prior_type == "bounded-normal":
        return BoundedNormal(
            mu=mu,
            sigma=prior_sigma,
            pmin=prior_lower_bound,
            pmax=prior_upper_bound,
            size=num_params,
        )
    elif prior_type == "uniform":
        return parameter.Uniform(prior_lower_bound, prior_upper_bound, size=num_params)
    else:
        raise ValueError(
            "prior_type can only be uniform or bounded-normal, not ", prior_type
        )


def filter_Mmat(psr, ltm_list=[]):
    """Filters the pulsar's design matrix of parameters
    :param psr: Pulsar object
    :param ltm_list: a list of parameters that will linearly varied, default is to vary anything not in tm_param_list
    :return: A new pulsar object with the filtered design matrix
    """
    idx_lin_pars = [psr.fitpars.index(p) for p in psr.fitpars if p in ltm_list]
    psr.fitpars = list(np.array(psr.fitpars)[idx_lin_pars])
    psr._designmatrix = psr._designmatrix[:, idx_lin_pars]
    return psr


# timing model delay
@signal_base.function
def tm_delay(t2pulsar, tm_params_orig, tm_param_dict={}, **kwargs):
    """
    Compute difference in residuals due to perturbed timing model.
    :param residuals: original pulsar residuals from Pulsar object
    :param t2pulsar: libstempo pulsar object
    :param tm_params_orig: dictionary of TM parameter tuples, (val, err)
    :param tm_params: new timing model parameters, rescaled to be in sigmas
    :param which: option to have all or only named TM parameters varied
    :return: difference between new and old residuals in seconds
    """
    """OUTLINE:
    take in parameters in par file
    save to dictionary
    Based on params in input param list, set parameter prior distribution
    Feed the priors and param list into tm_delay function
    """
    residuals = t2pulsar.residuals()
    # grab original timing model parameters and errors in dictionary
    orig_params = {}
    tm_params_rescaled = {}
    error_pos = {}
    for tm_scaled_key, tm_scaled_val in kwargs.items():
        if "DMX" in tm_scaled_key:
            tm_param = "_".join(tm_scaled_key.split("_")[-2:])
        else:
            tm_param = tm_scaled_key.split("_")[-1]
        orig_params[tm_param] = tm_params_orig[tm_param][0]

        if "physical" in tm_params_orig[tm_param]:
            # User defined priors are assumed to not be scaled
            tm_params_rescaled[tm_param] = tm_scaled_val
        else:
            tm_params_rescaled[tm_param] = (
                tm_scaled_val * tm_params_orig[tm_param][1]
                + tm_params_orig[tm_param][0]
            )

    # set to new values
    t2pulsar.vals(tm_params_rescaled)
    new_res = t2pulsar.residuals()

    # remeber to set values back to originals
    t2pulsar.vals(orig_params)

    # Return the time-series for the pulsar
    return new_res - residuals


# Model component building blocks #


def timing_block(
    psr,
    tm_param_list=["F0", "F1"],
    ltm_list=["Offset"],
    prior_type="uniform",
    prior_mu=0.0,
    prior_sigma=2.0,
    prior_lower_bound=-5.0,
    prior_upper_bound=5.0,
    tm_param_dict={},
    fit_remaining_pars=True,
):
    """
    Returns the timing model block of the model
    :param tm_param_list: a list of parameters to vary in the model
    :param prior_type: prior on timing parameters. Default is a bounded normal, can be "uniform"
    :param prior_sigma: Sets the center value on timing parameters for normal distribution draws
    :param prior_sigma: Sets the sigma on timing parameters for normal distribution draws
    :param prior_lower_bound: Sets the lower bound on timing parameters for bounded normal and uniform distribution draws
    :param prior_upper_bound: Sets the upper bound on timing parameters for bounded normal and uniform distribution draws
    :param tm_param_dict: a nested dictionary of parameters to vary in the model and their user defined values and priors:
        e.g. {'PX':{'prior_sigma':prior_sigma,'prior_lower_bound':prior_lower_bound,'prior_upper_bound':prior_upper_bound}}
        The priors cannot be normalized by sigma if there are uneven error bounds!
    """
    # If param in tm_param_dict not in tm_param_list, add it
    for key in tm_param_dict.keys():
        if key not in tm_param_list:
            tm_param_list.append(key)

    physical_tm_priors = get_default_physical_tm_priors()

    # Get values and errors as pulled by libstempo from par file.
    ptypes = ["normalized" for ii in range(len(psr.t2pulsar.pars()))]
    psr.tm_params_orig = OrderedDict(
        zip(
            psr.t2pulsar.pars(),
            map(list, zip(psr.t2pulsar.vals(), psr.t2pulsar.errs(), ptypes)),
        )
    )

    # Check to see if nan or inf in pulsar parameter errors.
    # The refit will populate the incorrect errors, but sometimes
    # changes the values by too much, which is why it is done in this order.
    if np.any(np.isnan(psr.t2pulsar.errs())) or np.any(
        [err == 0.0 for err in psr.t2pulsar.errs()]
    ):
        eidxs = np.where(
            np.logical_or(np.isnan(psr.t2pulsar.errs()), psr.t2pulsar.errs() == 0.0)
        )[0]
        psr.t2pulsar.fit()
        for idx in eidxs:
            par = psr.t2pulsar.pars()[idx]
            psr.tm_params_orig[par][1] = psr.t2pulsar.errs()[idx]

    tm_delay_kwargs = {}
    default_prior_params = [prior_mu, prior_sigma, prior_lower_bound, prior_upper_bound]
    for par in tm_param_list:
        if par == "Offset":
            raise ValueError(
                "TEMPO2 does not support modeling the phase offset: 'Offset'."
            )
        elif par in tm_param_dict.keys():
            # Overwrite default priors if new ones defined for the parameter in tm_param_dict
            psr.tm_params_orig[par][-1] = "physical"
            if "prior_mu" in tm_param_dict[par].keys():
                prior_mu = tm_param_dict[par]["prior_mu"]
            else:
                prior_mu = default_prior_params[0]
            if "prior_sigma" in tm_param_dict[par].keys():
                prior_sigma = tm_param_dict[par]["prior_sigma"]
            else:
                prior_sigma = default_prior_params[1]
            if "prior_lower_bound" in tm_param_dict[par].keys():
                prior_lower_bound = tm_param_dict[par]["prior_lower_bound"]
            else:
                val, err, _ = psr.tm_params_orig[par]
                prior_lower_bound = np.float(val + err * prior_lower_bound)
            if "prior_upper_bound" in tm_param_dict[par].keys():
                prior_upper_bound = tm_param_dict[par]["prior_upper_bound"]
            else:
                val, err, _ = psr.tm_params_orig[par]
                prior_upper_bound = np.float(val + err * prior_upper_bound)
        else:
            prior_mu = default_prior_params[0]
            prior_sigma = default_prior_params[1]
            prior_lower_bound = default_prior_params[2]
            prior_upper_bound = default_prior_params[3]

        if par in physical_tm_priors.keys():
            if par in tm_param_dict.keys():
                if "pmin" in physical_tm_priors[par].keys():
                    if prior_lower_bound < physical_tm_priors[par]["pmin"]:
                        prior_lower_bound = physical_tm_priors[par]["pmin"]
                if "pmax" in physical_tm_priors[par].keys():
                    if prior_upper_bound > physical_tm_priors[par]["pmax"]:
                        prior_upper_bound = physical_tm_priors[par]["pmax"]
            else:
                val, err, _ = psr.tm_params_orig[par]
                if "pmin" in physical_tm_priors[par].keys():
                    if val + err * prior_lower_bound < physical_tm_priors[par]["pmin"]:
                        psr.tm_params_orig[par][-1] = "physical"
                        prior_lower_bound = physical_tm_priors[par]["pmin"]
                if "pmax" in physical_tm_priors[par].keys():
                    if val + err * prior_upper_bound > physical_tm_priors[par]["pmax"]:
                        psr.tm_params_orig[par][-1] = "physical"
                        prior_upper_bound = physical_tm_priors[par]["pmax"]

        tm_delay_kwargs[par] = get_prior(
            prior_type,
            prior_sigma,
            prior_lower_bound,
            prior_upper_bound,
            mu=prior_mu,
        )
    # timing model

    tm_func = tm_delay(tm_param_dict=tm_param_dict, **tm_delay_kwargs)
    tm = deterministic_signals.Deterministic(tm_func, name="timing_model")

    # filter design matrix of all but linear params
    if fit_remaining_pars:
        if not ltm_list:
            ltm_list = [p for p in psr.fitpars if p not in tm_param_list]
        filter_Mmat(psr, ltm_list=ltm_list)
        ltm = gp_signals.TimingModel(coefficients=False)
        tm += ltm

    return tm
