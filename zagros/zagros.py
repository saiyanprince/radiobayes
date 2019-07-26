# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import pyrap.tables as pt

from vardefs import *
from priors import Priors
from africanus.rime.cuda import phase_delay, predict_vis
from africanus.coordinates import radec_to_lm
from africanus.model.coherency.cuda import convert

# Global variables related to input data
data_vis = None # variable to hold input data matrix
data_uvw = None
data_ant1 = None
data_ant2 = None
data_inttime = None
data_flag = None
data_flag_row = None

data_nant = None
data_nbl = None
data_uniqtime_index = None

data_nchan = None
data_chanwidth = None
data_chan_freq=None # Frequency of each channel. NOTE: Can handle only one SPW.

# Global variables to be computed / used for bookkeeping
baseline_dict = None # Constructed in main()
init_loglike = False # To initialise the loglike function
ndata_unflgged = None
per_bl_sig = None
weight_vector = None

# Other global vars that will be set through command-line
hypo = None
npsrc = None
ngsrc = None

def create_parser():
    p = argparse.ArgumentParser()
    p.add_argument("ms", help="Input MS name")
    p.add_argument("col", help="Name of the data column from MS")
    p.add_argument("-iuvw", "--invert-uvw", action="store_true",
                   help="Invert UVW coordinates. Necessary to compare"
                        "codex vis against MeqTrees-generated vis")
    p.add_argument('--hypo', type=int, choices=[0,1,2], required=True)
    p.add_argument('--npsrc', type=int, required=True)
    p.add_argument('--ngsrc', type=int, required=True)
    p.add_argument('--npar', type=int, required=True)
    p.add_argument('--basedir', type=str, required=True)
    p.add_argument('--fileroot', type=str, required=True)
    return p

def pol_to_rec(amp, phase):
    re = amp*np.cos(phase*np.pi/180.0)
    im = amp*np.sin(phase*np.pi/180.0)
    return re, im

# INI: Flicked from MeqSilhouette
def make_baseline_dictionary(ant_unique):
    return dict([((x, y), np.where((data_ant1 == x) & (data_ant2 == y))[0]) for x in ant_unique for y in ant_unique if y > x])

# INI: For handling different correlation schema; not used as of now
def corr_schema():
    """
    Parameters
    ----------
    None

    Returns
    -------
    corr_schema : list of list
        correlation schema from the POLARIZATION table,
        `[[9, 10], [11, 12]]` for example
    """

    corrs = pol.NUM_CORR.values
    corr_types = pol.CORR_TYPE.values

    if corrs == 4:
        return [[corr_types[0], corr_types[1]],
                [corr_types[2], corr_types[3]]]  # (2, 2) shape
    elif corrs == 2:
        return [corr_types[0], corr_types[1]]    # (2, ) shape
    elif corrs == 1:
        return [corr_types[0]]                   # (1, ) shape
    else:
        raise ValueError("corrs %d not in (1, 2, 4)" % corrs)

def einsum_schema():
    """
    Returns an einsum schema suitable for multiplying per-baseline
    phase and brightness terms.
    Parameters
    ----------
    None

    Returns
    -------
    einsum_schema : str
    """
    corrs = data_vis.shape[2]

    if corrs == 4:
        return "srf, sij -> srfij"
    elif corrs in (2, 1):
        return "srf, si -> srfi"
    else:
        raise ValueError("corrs %d not in (1, 2, 4)" % corrs)

def loglike(theta):
    """
    Compute the loglikelihood function
    Parameters
    ----------
    theta : Input parameter vector

    Returns
    -------
    loglike : float
    """

    global init_loglike, ndata_unflagged, per_bl_sig, weight_vector, data_vis

    if init_loglike == False:

        # Find total number of visibilities
        ndata = data_vis.shape[0]*data_vis.shape[1]*data_vis.shape[2]*2 # 8 because each polarisation has two real numbers (real & imaginary)
        flag_ll = np.logical_not(data_flag[:,0,0])
        ndata_unflagged = ndata - np.where(flag_ll == False)[0].shape[0] * 8
        print ('Percentage of unflagged visibilities: ', ndata_unflagged, '/', ndata, '=', (ndata_unflagged/ndata)*100)

        # Set visibility weights
        weight_vector=np.zeros(data_vis.shape, dtype='float') # ndata/2 because the weight_vector is the same for both real and imag parts of the vis.
        if not sigmaSim:
            per_bl_sig = np.zeros((data_nbl))
            bl_incr = 0;
            for a1 in np.arange(data_nant):
              for a2 in np.arange(a1+1,data_nant):
                #per_bl_sig[bl_incr] = np.sqrt((sefds[a1]*sefds[a2])/(data_chanwidth*data_inttime[bl_incr])) # INI: Removed the sq(2) from the denom. It's for 2 pols.
                per_bl_sig[bl_incr] = np.sqrt((sefds[a1]*sefds[a2])/(2*data_chanwidth*data_inttime[bl_incr])) # INI: Added the sq(2) bcoz MeqS uses this convention
                weight_vector[baseline_dict[(a1,a2)]] = 1.0 / np.power(per_bl_sig[bl_incr], 2)
                bl_incr += 1;
        else:
            weight_vector[:] = 1.0 /np.power(sigmaSim, 2)

        weight_vector *= np.logical_not(data_flag)
        weight_vector = cp.array(weight_vector.reshape((data_vis.shape[0], data_vis.shape[1], 2, 2)))

        init_loglike = True # loglike initialised; will not enter on subsequent iterations

    # Set up arrays necessary for forward modelling
    # Set up the phase delay matrix
    lm = cp.array([[theta[1], theta[2]]])
    phase = phase_delay(lm, data_uvw, data_chan_freq)

    # Set up the brightness matrix
    stokes = cp.array([[theta[0], 0, 0, 0]])
    brightness =  convert(stokes, ['I', 'Q', 'U', 'V'], [['RR', 'RL'], ['LR', 'LL']])

    # Compute einsum schema
    einschema = einsum_schema()

    # Compute the source coherency matrix (the uncorrupted visibilities, except for the phase delay)
    source_coh_matrix =  cp.einsum(einschema, phase, brightness)

    # Predict (forward model) visibilities
    model_vis = predict_vis(data_uniqtime_index, data_ant1, data_ant2, None, source_coh_matrix, None, None, None, None)

    # Compute chi-squared and loglikelihood
    diff = model_vis - data_vis.reshape((data_vis.shape[0], data_vis.shape[1], 2, 2))
    chi2 = cp.sum((diff.real*diff.real+diff.imag*diff.imag) * weight_vector)
    loglike = cp.float(-chi2/2.0 - cp.log(2*cp.pi*(1.0/weight_vector.flatten()[cp.nonzero(weight_vector.flatten())])).sum())

    return loglike, []

#------------------------------------------------------------------------------
pri=None
def prior_transform(hcube):
    """
    Transform the unit hypercube into the prior ranges and distributions requested
    """

    global pri;
    if pri is None: pri=Priors()

    theta = []

    if hypo == 0:
        theta.append(pri.GeneralPrior(hcube[0],'U',Smin,Smax))
        theta.append(pri.GeneralPrior(hcube[1],'U',dxmin,dxmax))
        theta.append(pri.GeneralPrior(hcube[2],'U',dymin,dymax))

    else:
        print('*** WARNING: Illegal hypothesis')
        return None

    return theta
#------------------------------------------------------------------------------

def main(args):

    global hypo, npsrc, ngsrc, data_vis, data_uvw, data_nant, data_nbl, data_uniqtime_index, data_ntime, data_inttime, \
            data_chan_freq, data_nchan, data_chanwidth, data_flag, data_flag_row, data_ant1, data_ant2, baseline_dict

    # Set command line parameters
    hypo = args.hypo
    npsrc = args.npsrc
    ngsrc = args.ngsrc

    ####### Read data from MS
    tab = pt.table(args.ms).query("ANTENNA1 != ANTENNA2"); # INI: always exclude autocorrs; this code DOES NOT work for autocorrs
    data_vis = tab.getcol(args.col)
    data_ant1 = tab.getcol('ANTENNA1')
    data_ant2 = tab.getcol('ANTENNA2')
    ant_unique = np.unique(np.hstack((data_ant1, data_ant2)))
    baseline_dict = make_baseline_dictionary(ant_unique)

    # Read uvw coordinates; nececssary for computing the source coherency matrix
    data_uvw = tab.getcol('UVW')
    if args.invert_uvw: data_uvw = -data_uvw # Invert uvw coordinates for comparison with MeqTrees

    # get data from ANTENNA subtable
    anttab = pt.table(args.ms+'/ANTENNA')
    stations = anttab.getcol('STATION')
    data_nant = len(stations)
    data_nbl = int((data_nant*(data_nant-1))/2)
    anttab.close()

    # Obtain indices of unique times in 'TIME' column
    _, data_uniqtime_index = np.unique(tab.getcol('TIME'), return_inverse=True)
    data_inttime = tab.getcol('EXPOSURE', 0, data_nbl)

    # Get flag info from MS
    data_flag = tab.getcol('FLAG')
    data_flag_row = tab.getcol('FLAG_ROW')
    data_flag = np.logical_or(data_flag, data_flag_row[:,np.newaxis,np.newaxis])

    tab.close()

    # get frequency info from SPECTRAL_WINDOW subtable
    freqtab = pt.table(args.ms+'/SPECTRAL_WINDOW')
    data_chan_freq = freqtab.getcol('CHAN_FREQ')[0]
    data_nchan = freqtab.getcol('NUM_CHAN')[0]
    data_chanwidth = freqtab.getcol('CHAN_WIDTH')[0,0];
    freqtab.close();

    # Move necessary arrays to cupy from numpy
    data_vis = cp.array(data_vis)
    data_ant1 = cp.array(data_ant1)
    data_ant2 = cp.array(data_ant2)
    data_uvw = cp.array(data_uvw)
    data_uniqtime_index = cp.array(data_uniqtime_index, dtype=cp.int32)
    data_chan_freq = cp.array(data_chan_freq)

    '''# Set up pypolychord
    settings = PolyChordSettings(args.npar, 0)
    settings.base_dir = args.basedir
    settings.file_root = args.fileroot
    settings.nlive = nlive
    settings.num_repeats = num_repeats
    settings.precision_criterion = evtol
    settings.do_clustering = False # check whether this works with MPI
    settings.read_resume = False
    settings.seed = seed    

    ppc.run_polychord(loglike, args.npar, 0, settings, prior=prior_transform)'''

    # Make a callable for running PolyChord
    my_callable = dyPolyChord.pypolychord_utils.RunPyPolyChord(loglike, prior_transform, args.npar)

    dynamic_goal = 1.0  # whether to maximise parameter estimation or evidence accuracy.
    ninit = nlive_init          # number of live points to use in initial exploratory run.
    nlive_const = nlive   # total computational budget is the same as standard nested sampling with nlive_const live points.
    settings_dict = {'file_root': args.fileroot,
                     'base_dir': args.basedir,
                     'seed': seed}
    
    comm = MPI.COMM_WORLD

    # Run dyPolyChord
    dyPolyChord.run_dypolychord(my_callable, dynamic_goal, settings_dict, ninit=ninit, nlive_const=nlive_const, comm=comm)

    return 0

if __name__ == '__main__':
    args = create_parser().parse_args()
    ret = main(args)
    sys.exit(ret)