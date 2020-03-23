import numpy as np
import copy
from sklearn.ensemble import IsolationForest
from scipy.interpolate import interp1d
from scipy.signal import medfilt
from scipy.integrate import simps


# Classes and functions to support the galaxies, filters and Sed's
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
class Galaxy:
    '''Class defining a galaxy'''
    def __init__(self, wavelen=None, mags=None, mag_err=None, fluxes=None, 
                flux_err=None, filters=None, redshift=None, source=None):
        self.wavelen = wavelen
        self.mags = mags
        self.mag_err = mag_err
        self.fluxes = fluxes
        self.flux_err = flux_err
        self.filters = filters
        self.redshift = redshift
        self.source = source

    _mag_ref = 25
    _lambda_ref = 5000 # Angstrom
    def magToflux(self):
        self.fluxes = (self._lambda_ref/self.wavelen)**2 * 10**((self._mag_ref-self.mags)/2.5)
        self.flux_err = self.fluxes/2.5 * np.log(10) * self.mag_err
    def fluxTomag(self):
        self.mags = self._mag_ref - 2.5*np.log10((self.wavelen/self._lambda_ref)**2 * self.fluxes)
        self.mag_err = 2.5/np.log(10) * self.flux_err/self.fluxes
    

class Bandpass:
    '''Class defining a bandpass filter'''
    def __init__(self, filename):
        # load from file
        wavelen,sb = np.loadtxt(filename,unpack=True)
        # resample wavelen and calculate phi
        self.wavelen = np.arange(min(wavelen),max(wavelen),20)
        sb = np.interp(self.wavelen,wavelen,sb)
        self.phi = sb/self.wavelen
        self.phi /= self.phi.sum() * (self.wavelen[1] - self.wavelen[0])
        del wavelen,sb
        # calculate effective wavelen
        self.eff_wavelen = (self.wavelen*self.phi).sum()/self.phi.sum()
        
def get_bandpass_dict():
    names, files = np.loadtxt('filters/filters.list', unpack=True, dtype=str)
    bandpass_dict = dict()
    for i,filename in enumerate(files):
        bandpass = Bandpass('filters/'+filename)
        bandpass_dict[names[i]] = bandpass  
    return bandpass_dict

def get_eff_wavelen(bandpass_dict, filters=None):
    if filters is None:
        filters = bandpass_dict.keys()
    eff_wavelen = []
    for name in filters:
        band = bandpass_dict[name]
        eff_wavelen.append(band.eff_wavelen)
    return np.array(eff_wavelen)
    

class Sed:
    '''Class defining an SED'''
    def __init__(self, wavelen=None, flambda=None):
        self.wavelen = wavelen
        self.flambda = flambda
        
    def redshift(self, z):
        self.wavelen *= (1 + z)
        
    def flux(self, bandpass):
        y = np.interp(bandpass.wavelen,self.wavelen,self.flambda)
        flux = (y*bandpass.phi).sum() * (bandpass.wavelen[1] - bandpass.wavelen[0])
        return flux
    
    def fluxlist(self, bandpass_dict, filters=None):
        if filters is None:
            filters = bandpass_dict.keys()
        fluxes = []
        for name in filters:
            bandpass = bandpass_dict[name]
            fluxes.append(self.flux(bandpass))
        return np.array(fluxes)
    
    
    
# Functions for assembling training sets
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
def match_photometry(template_dict,galaxy,bandpass_dict):
    
    keys = np.array(list(template_dict.keys()))
    mse_list = np.array([])
    scales = np.array([])
    
    for template in template_dict.values():

        idx = np.where( (galaxy.filters != 'Ks') & (galaxy.filters != 'Kvideo') )
        filters = galaxy.filters[idx]
        fluxes = galaxy.fluxes[idx]
        errs = galaxy.flux_err[idx]/galaxy.fluxes[idx]
        
        sed = copy.deepcopy(template)
        sed.redshift(galaxy.redshift)
        template_fluxes = sed.fluxlist(bandpass_dict, filters)
        
        scale = np.median(template_fluxes/fluxes)
        idx = (np.fabs(template_fluxes/fluxes - scale)).argmin()
        template_fluxes /= template_fluxes[idx]
        fluxes /= fluxes[idx]
        
        mse = np.mean(1/errs**2*(template_fluxes - fluxes)**2)
        mse_list = np.append(mse_list,mse)
        scales = np.append(scales,scale)
        
    idx = mse_list.argmin()
    return keys[idx],scales[idx]

def create_training_sets(template_dict,galaxies,bandpass_dict):
    
    # create a dictionary to hold the set for each template
    sets = dict()
    for key in template_dict.keys():
        sets[key] = []
        
    # sort each photometry into the training sets
    for galaxy in galaxies:

        match, scale = match_photometry(template_dict,galaxy,bandpass_dict)
        
        wavelen = galaxy.wavelen/(1+galaxy.redshift)
        
        for i in range(len(wavelen)):
            sets[match].append([wavelen[i],galaxy.fluxes[i]*scale,
                                galaxy.flux_err[i]*scale,galaxy.redshift,
                                galaxy.filters[i]])
            
    return sets



# Functions for training templates
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
def perturb_template(template, training_set, bandpass_dict, w=0.75, Delta=None):
    """
    Function that perturbs an SED template in accordance with the
    matched photometry. Definition of terms found in paper I am writing.
    """
    
    nbins = len(template.wavelen)
    wavelen = template.wavelen
    widths = np.array([float(wavelen[1]-wavelen[0]),
                      *[(wavelen[i+1]-wavelen[i-1])/2 for i in range(1,nbins-1)],
                      float(wavelen[-1]-wavelen[-2])])
    
    # create initial M and nu
    if Delta is None:
        sigmas = np.array([i[2] for i in training_set])/np.array([i[1] for i in training_set])
        Delta = np.mean(sigmas)*np.sqrt(len(template.wavelen)/(w*len(training_set)))
        Delta = np.clip(Delta,0,0.05)
    M = np.identity(nbins)*1/Delta**2

    nu = np.zeros(nbins)
    
    # run through all the photometry
    for i,row in enumerate(training_set):
            
        # get observed flux
        obs_flux = row[1]
        sigma = row[2]/obs_flux
        
        # calculate template flux
        redshift = row[3]
        filter_name = row[4]
        template_ = copy.deepcopy(template)
        template_.redshift(redshift)
        template_flux = template_.flux(bandpass_dict[filter_name])
        
        # calculate r^n * Delta lambda
        rn = np.interp(wavelen * (1+redshift), 
                        bandpass_dict[filter_name].wavelen, 
                        bandpass_dict[filter_name].phi)
        dlambda = widths * (1+redshift)
        rn_dlambda = rn * dlambda
        
        # add to M
        M += 1/sigma**2 * np.outer(rn_dlambda,rn_dlambda)
        
        # add to nu
        nu += 1/sigma**2 * (obs_flux - template_flux) * rn_dlambda
    
        
    # solve the system for the perturbation    
    sol = np.linalg.solve(M,nu)
    return sol


def train_templates(template_dict, galaxies, bandpass_dict, N_rounds=5, 
                    N_iter=1, w=0.75, Delta=None):
    
    new_templates = copy.deepcopy(template_dict)

    old_training_sets = dict()
    for key in template_dict.keys():
        old_training_sets[key] = None

    for i in range(N_rounds):
        
        print("Round "+str(i+1)+"/"+str(N_rounds))
        
        training_sets = create_training_sets(new_templates,galaxies,bandpass_dict)
        
        for key in new_templates.keys():
            template = new_templates[key]
            training_set = training_sets[key]

            # remove outliers
            x = np.array(training_set)[:,0].astype(float)
            y = np.array(training_set)[:,1].astype(float)
            clf = IsolationForest(max_samples=len(x))
            xy = np.array([x,y]).T
            idx = np.where( clf.fit_predict(xy) == 1 )
            training_set = [training_set[j] for j in idx[0]]

            if training_set == old_training_sets[key]:
                print("Skipping",key)
                continue

            for j in range(N_iter):
                pert = perturb_template(template,training_set,bandpass_dict,w=w,Delta=Delta)
                template.flambda += pert
                template.flambda = np.clip(template.flambda,a_min=0,a_max=None)

            old_training_sets[key] = training_set

    # now, re-normalize all the templates at 10,000 angstroms
    for template in new_templates.values():
        scale = np.interp(10000, template.wavelen, template.flambda)
        template.flambda /= scale
        
    print("Generating final sets")
    training_sets = create_training_sets(new_templates,galaxies,bandpass_dict)
    print("Done!")

    return new_templates, training_sets



# Functions for spectral lines
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
def idx_closest(x,array):
    return np.fabs(np.array(array)-x).argmin()

def get_continuum(sed,window,buffer=500,order=2):
    
    idxlo1 = idx_closest(window[0]-buffer,sed.wavelen)
    idxhi1 = idx_closest(window[0],sed.wavelen)  
    idxlo2 = idx_closest(window[1],sed.wavelen)
    idxhi2 = idx_closest(window[1]+buffer,sed.wavelen)
    
    x = np.concatenate((sed.wavelen[idxlo1:idxhi1],sed.wavelen[idxlo2:idxhi2]))
    y = np.concatenate((sed.flambda[idxlo1:idxhi1],sed.flambda[idxlo2:idxhi2]))
    
    continuum = np.polynomial.Chebyshev.fit(x,y,deg=order)
    
    return continuum

def get_subtracted(sed,window,buffer=500,order=2):
    
    subtracted = copy.deepcopy(sed)
    
    idxlo = idx_closest(window[0],subtracted.wavelen)
    idxhi = idx_closest(window[1],subtracted.wavelen)
    
    subtracted.flambda[:idxlo] *= 0
    subtracted.flambda[idxhi:] *= 0
    
    continuum = get_continuum(sed,window,buffer,order)
    subtracted.flambda[idxlo:idxhi] -= continuum(subtracted.wavelen[idxlo:idxhi])
    
    return subtracted

def line_photometry(x,wavelen,bandpass_dict,FWHM=30):
    
    sig = FWHM/2.355
    
    lineSED = Sed(x,0*x)
    gaussian = lambda x: np.exp(-(x-wavelen)**2/(2*sig**2))
    lineSED.flambda += gaussian(lineSED.wavelen)

    wavelens = np.array([])
    fluxes = np.array([])
    for z in np.linspace(0,3,500):
        lineSED_ = copy.deepcopy(lineSED)
        lineSED_.redshift(z)
        bands = ['y']
        wavelens_ = [bandpass_dict[band].eff_wavelen/(1+z) for band in bands]
        fluxes_ = lineSED_.fluxlist(bandpass_dict,bands)
        wavelens = np.concatenate((wavelens,wavelens_))
        fluxes = np.concatenate((fluxes,fluxes_))
        
    idx = np.argsort(wavelens)
    wavelens = wavelens[idx]
    fluxes = medfilt(fluxes[idx],kernel_size=11)
    
    f = interp1d(wavelens,fluxes,bounds_error=False,fill_value=0)
    return f 

def spectral_lines(sed,bandpass_dict,FWHM=30,order=2):
    
    # make the SED that will have the spectral lines
    continuum_sed = copy.deepcopy(sed)
    line_sed = copy.deepcopy(sed)
    line_sed.flambda *= 0
    
    sig = FWHM/2.355
    
    # Will go through each spectral line
    # First, will get the SED minus the continuum
    # Then, will calculate the scale factor needed to account for the flux
    # Finally, will subtract the line convolved with the filters,
    # After I have done that for every line, I will add all the lines in
    
    line_dict = {
        "Halpha": { "wavelen": 6563,
                    "window" : [5850,7300],
                    "scale"  : None,
                    "ratio"  : None,
                    "ref"    : None,
                    "buffer" : 500 },
        "Hbeta" : { "wavelen": 4861,
                    "window" : None,
                    "scale"  : None,
                    "ratio"  : 1/2.9,
                    "ref"    : "Halpha",
                    "buffer" : None },
        "O3"    : { "wavelen": 5007,
                    "window" : [4400,5600],
                    "scale"  : None,
                    "ratio"  : None,
                    "ref"    : None,
                    "buffer" : 300 },
        "O2"    : { "wavelen": 3727,
                    "window" : [3300,4200],
                    "scale"  : None,
                    "ratio"  : None,
                    "ref"    : None, 
                    "buffer" : 300}
    }
    
    for line in line_dict:
        
        wavelen, window, scale, ratio, ref, buffer = line_dict[line].values()
        
        if ref != None:
            scale = line_dict[ref]["scale"] * ratio
        else:
            subtracted = get_subtracted(continuum_sed,window,buffer=buffer,order=order)
            scale = simps(subtracted.flambda,subtracted.wavelen)/(np.sqrt(2*np.pi)*sig)
          
        scale = np.clip(scale,0,None)
        line_dict[line]["scale"] = scale
        
        f = line_photometry(continuum_sed.wavelen,wavelen,bandpass_dict,FWHM=FWHM)
        continuum_sed.flambda -= scale*f(continuum_sed.wavelen)
        
        line_sed.flambda += scale * np.exp(-(line_sed.wavelen-wavelen)**2/(2*sig**2))
        
    final_sed = copy.deepcopy(continuum_sed)
    final_sed.flambda += line_sed.flambda
        
    return final_sed, continuum_sed, line_sed