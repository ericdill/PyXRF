# ######################################################################
# Copyright (c) 2014, Brookhaven Science Associates, Brookhaven        #
# National Laboratory. All rights reserved.                            #
#                                                                      #
# Redistribution and use in source and binary forms, with or without   #
# modification, are permitted provided that the following conditions   #
# are met:                                                             #
#                                                                      #
# * Redistributions of source code must retain the above copyright     #
#   notice, this list of conditions and the following disclaimer.      #
#                                                                      #
# * Redistributions in binary form must reproduce the above copyright  #
#   notice this list of conditions and the following disclaimer in     #
#   the documentation and/or other materials provided with the         #
#   distribution.                                                      #
#                                                                      #
# * Neither the name of the Brookhaven Science Associates, Brookhaven  #
#   National Laboratory nor the names of its contributors may be used  #
#   to endorse or promote products derived from this software without  #
#   specific prior written permission.                                 #
#                                                                      #
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS  #
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT    #
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS    #
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE       #
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,           #
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES   #
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR   #
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)   #
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,  #
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OTHERWISE) ARISING   #
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE   #
# POSSIBILITY OF SUCH DAMAGE.                                          #
########################################################################

__author__ = 'Li Li'

import numpy as np
import time
import copy
import six
import os
from collections import OrderedDict
import multiprocessing

from atom.api import Atom, Str, observe, Typed, Int, List, Dict, Float
from skxray.fitting.xrf_model import (ModelSpectrum, update_parameter_dict,
                                      sum_area, set_parameter_bound, ParamController,
                                      nnls_fit, weighted_nnls_fit, trim,
                                      construct_linear_model, linear_spectrum_fitting,
                                      compute_escape_peak, register_strategy)
from skxray.fitting.background import snip_method
from pyxrf.model.guessparam import (calculate_profile, fit_strategy_list)
from lmfit import fit_report

import logging
logger = logging.getLogger(__name__)


class Fit1D(Atom):

    #file_path = Str()
    file_status = Str()
    param_dict = Dict()

    element_list = List()
    #parameters = Dict()

    data_all = Typed(np.ndarray)
    data = Typed(np.ndarray)
    fit_x = Typed(np.ndarray)
    fit_y = Typed(np.ndarray)
    residual = Typed(np.ndarray)
    comps = Dict()
    fit_strategy1 = Int(0)
    fit_strategy2 = Int(0)
    fit_strategy3 = Int(0)
    fit_strategy4 = Int(0)
    fit_strategy5 = Int(0)
    fit_result = Typed(object)
    data_title = Str()
    result_folder = Str()

    all_strategy = Typed(object) #Typed(OrderedDict)

    x0 = Typed(np.ndarray)
    y0 = Typed(np.ndarray)
    bg = Typed(np.ndarray)
    es_peak = Typed(np.ndarray)
    cal_x = Typed(np.ndarray)
    cal_y = Typed(np.ndarray)
    cal_spectrum = Dict()

    # attributes used by the ElementEdit window
    selected_element = Str()
    selected_elements = List()

    def __init__(self, *args, **kwargs):
        self.result_folder = kwargs['working_directory']
        self.all_strategy = OrderedDict()

    @observe('selected_element')
    def _selected_element_changed(self, changed):
        element = self.selected_element.split('_')[0]
        self.selected_elements = sorted([e for e in self.param_dict.keys()
                                        if element in e])

    def get_new_param(self, param):
        self.param_dict = copy.deepcopy(param)
        element_list = self.param_dict['non_fitting_values']['element_list']
        self.element_list = [e.strip(' ') for e in element_list.split(',')]
        print('get new element list : {}'.format(self.element_list))

        # register the strategy and extend the parameter list
        # to cover all given elements
        for strat_name in fit_strategy_list:
            strategy = extract_strategy(self.param_dict, strat_name)
            # register the strategy and extend the parameter list
            # to cover all given elements
            register_strategy(strat_name, strategy)
            set_parameter_bound(self.param_dict, strat_name)

    @observe('data')
    def _update_data(self, change):
        self.data = np.asarray(self.data)

    @observe('fit_strategy1')
    def update_strategy1(self, change):
        print(change)
        self.all_strategy.update({'strategy1': change['value']})
        if change['value']:
            logger.info('Strategy at step 1 is: {}'.
                        format(fit_strategy_list[change['value']-1]))

    @observe('fit_strategy2')
    def update_strategy2(self, change):
        self.all_strategy.update({'strategy2': change['value']})
        if change['value']:
            logger.info('Strategy at step 2 is: {}'.
                        format(fit_strategy_list[change['value']-1]))

    @observe('fit_strategy3')
    def update_strategy3(self, change):
        self.all_strategy.update({'strategy3': change['value']})
        if change['value']:
            logger.info('Strategy at step 3 is: {}'.
                        format(fit_strategy_list[change['value']-1]))

    @observe('fit_strategy4')
    def update_strategy4(self, change):
        self.all_strategy.update({'strategy4': change['value']})
        if change['value']:
            logger.info('Strategy at step 4 is: {}'.
                        format(fit_strategy_list[change['value']-1]))

    @observe('fit_strategy5')
    def update_strategy5(self, change):
        self.all_strategy.update({'strategy5': change['value']})
        if change['value']:
            logger.info('Strategy at step 5 is: {}'.
                        format(fit_strategy_list[change['value']-1]))

    def update_param_with_result(self):
        update_parameter_dict(self.param_dict, self.fit_result)

    def define_range(self):
        """
        Cut x range according to values define in param_dict.
        """
        x = np.arange(self.data.size)
        # ratio to transfer energy value back to channel value
        approx_ratio = 100
        lowv = self.param_dict['non_fitting_values']['energy_bound_low']['value'] * approx_ratio
        highv = self.param_dict['non_fitting_values']['energy_bound_high']['value'] * approx_ratio
        self.x0, self.y0 = trim(x, self.data, lowv, highv)

    def get_background(self):
        self.bg = snip_method(self.y0,
                              self.param_dict['e_offset']['value'],
                              self.param_dict['e_linear']['value'],
                              self.param_dict['e_quadratic']['value'])

    def escape_peak(self):
        ratio = 0.005
        xe, ye = compute_escape_peak(self.data, ratio, self.param_dict)
        lowv = self.param_dict['non_fitting_values']['energy_bound_low']
        highv = self.param_dict['non_fitting_values']['energy_bound_high']
        xe, self.es_peak = trim(xe, ye, lowv, highv)
        logger.info('Escape peak is considered with ratio {}'.format(ratio))

        # align to the same length
        if self.y0.size > self.es_peak.size:
            temp = self.es_peak
            self.es_peak = np.zeros(len(self.y0.size))
            self.es_peak[:temp.size] = temp
        else:
            self.es_peak = self.es_peak[:self.y0.size]

    def get_profile(self):
        self.define_range()
        self.cal_x, self.cal_spectrum = calculate_profile(self.data, self.param_dict,
                                                          self.element_list)
        self.cal_y = np.zeros(len(self.cal_x))
        for k, v in six.iteritems(self.cal_spectrum):
            #print('component: {}'.format(k))
            self.cal_y += v
        self.residual = self.cal_y - self.y0

    def fit_data(self, x0, y0,
                 c_val=1e-2, fit_num=100, c_weight=1e3):
        MS = ModelSpectrum(self.param_dict, self.element_list)
        MS.assemble_models()

        result = MS.model_fit(x0, y0,
                              weights=1/np.sqrt(c_weight+y0), maxfev=fit_num,
                              xtol=c_val, ftol=c_val, gtol=c_val)

        self.comps.clear()
        comps = result.eval_components(x=x0)
        self.comps = combine_lines(comps, self.element_list, self.bg)

        xnew = (result.values['e_offset'] +
                result.values['e_linear'] * x0 +
                result.values['e_quadratic'] * x0**2)
        self.fit_x = xnew
        self.fit_y = result.best_fit
        self.fit_result = result
        self.residual = self.fit_y - y0

    def fit_multiple(self):
        """
        Fit data in sequence according to given strategies.
        The param_dict is extended to cover elemental parameters.
        """
        self.define_range()
        self.get_background()
        #self.escape_peak()

        #PC = ParamController(self.param_dict, self.element_list)
        #self.param_dict = PC.params
        #print('param keys {}'.format(self.param_dict.keys()))

        y0 = self.y0 - self.bg #- self.es_peak

        t0 = time.time()
        logger.info('Start fitting!')
        for k, v in six.iteritems(self.all_strategy):
            if v:
                strat_name = fit_strategy_list[v-1]
                logger.info('Fit with {}: {}'.format(k, strat_name))

                strategy = extract_strategy(self.param_dict, strat_name)
                # register the strategy and extend the parameter list
                # to cover all given elements
                register_strategy(strat_name, strategy)
                set_parameter_bound(self.param_dict, strat_name)

                self.fit_data(self.x0, y0)
                self.update_param_with_result()

        self.fit_y += self.bg #+ self.es_peak

        t1 = time.time()
        logger.warning('Time used for fitting is : {}'.format(t1-t0))
        self.save_result()

    def fit_single_pixel(self):
        """
        This function performs single pixel fitting. Multiprocess is considered.
        """
        strategy_pixel = 'linear'
        set_parameter_bound(self.param_dict, strategy_pixel)
        logger.info('Starting single pixel fitting')
        t0 = time.time()
        result_map = fit_pixel_fast_multi(self.data_all, self.param_dict)
        t1 = time.time()
        logger.warning('Time used for pixel fitting is : {}'.format(t1-t0))

        # save data
        fpath = os.path.join(self.result_folder, 'Root.h5')
        write_to_hdf(fpath, result_map)

        #import matplotlib.pyplot as plt
        #plt.imshow(result_map['Fe_K'])
        #plt.show()

        # currently save data using pickle, need to be updated
        import pickle
        fpath = os.path.join(self.result_folder, 'root_data')
        pickle.dump(result_map, open(fpath, 'wb'))

    def save_result(self, fname=None):
        """
        Parameters
        ----------
        fname : str, optional
            name of output file
        """
        if not fname:
            fname = self.data_title+'_out.txt'
        filepath = os.path.join(self.result_folder, fname)
        with open(filepath, 'w') as myfile:
            myfile.write(fit_report(self.fit_result, sort_pars=True))
            logger.warning('Results are saved to {}'.format(filepath))


def combine_lines(components, element_list, background):
    """
    Combine results for different lines of the same element.
    And also add background, compton and elastic.

    Parameters
    ----------
    components : dict
        output results from lmfit
    element_list : list
        list of elemental lines
    background : array
        background calculated in given range

    Returns
    -------
    dict :
        combined results for elements and other related peaks.
    """
    new_components = {}
    for e in element_list:
        e_temp = e.split('_')[0]
        intensity = 0
        for k, v in six.iteritems(components):
            if e_temp in k:
                intensity += v
        new_components[e] = intensity

    # add background and elastic
    new_components.update({'background': background})
    new_components.update({'compton': components['compton']})
    new_components.update({'elastic': components['elastic_']})
    return new_components


def extract_strategy(param, name):
    """
    Extract given strategy from param dict.

    Parameters
    ----------
    param : dict
        saving all parameters
    name : str
        strategy name

    Returns
    -------
    dict :
        with given strategy as value
    """
    return {k: v[name] for k, v in six.iteritems(param) if k != 'non_fitting_values'}


def fit_pixel_fast(data, param):
    """
    Single pixel fit of experiment data. No multiprocess is applied.

    .. warning :: This function is not optimized as it calls linear_spectrum_fitting function,
    where lots of repeated calculation are processed.

    Parameters
    ----------
    data : array
        3D data of experiment spectrum
    param : dict
        fitting parameters

    Returns
    -------
    dict :
        fitting values for all the elements
    """

    datas = data.shape

    x0 = np.arange(datas[2])

    elist = param['non_fitting_values']['element_list'].split(', ')
    elist = [e.strip(' ') for e in elist]
    elist = [e+'_K' for e in elist if ('_' not in e)]

    non_element = ['compton', 'elastic', 'background']
    total_list = elist + non_element

    result_map = dict()
    for v in total_list:
        result_map.update({v: np.zeros([datas[0], datas[1]])})

    for i in xrange(datas[0]):
        logger.info('Row number at {} out of total {}'.format(i, datas[0]))
        for j in xrange(datas[1]):
            #logger.info('Column number at {} out of total {}'.format(j, datas[1]))
            x, result = linear_spectrum_fitting(data[i, j, :], param,
                                                elemental_lines=elist, constant_weight=5)
            for v in total_list:
                if v in result:
                    result_map[v][i, j] = np.sum(result[v])

    return result_map


def fit_per_line(row_num, data, matv, param):
    """
    Fit experiment data for a given row.

    Parameters
    ----------
    row_num : int
        which row to fit
    data : array
        3D data of experiment spectrum
    param : dict
        fitting parameters

    Returns
    -------
    array :
        fitting values for all the elements at a given row.
    """
    datas = data.shape
    logger.info('Row number is {}'.format(row_num))
    out = []
    for i in range(datas[1]):
        bg = snip_method(data[row_num, i, :],
                         param['e_offset']['value'],
                         param['e_linear']['value'],
                         param['e_quadratic']['value'])
        y = data[row_num, i, :] - bg
        result, res = fit_pixel(y, matv, weight=True)
        result = list(result)# + [np.sum(bg)]
        out.append(result)
    return np.array(out)


def fit_pixel_fast_multi(data, param):
    """
    Multiprocess fit of experiment data.

    Parameters
    ----------
    data : array
        3D data of experiment spectrum
    param : dict
        fitting parameters

    Returns
    -------
    dict :
        fitting values for all the elements
    """

    #logger.info('Row number at {} out of total {}'.format(i, datas[0]))
    #logger.info('no_processors_to_use = {}'.format(no_processors_to_use))
    no_processors_to_use = multiprocessing.cpu_count()
    logger.info('cpu count: {}'.format(no_processors_to_use))
    #print 'Creating pool with %d processes\n' % no_processors_to_use
    pool = multiprocessing.Pool(no_processors_to_use)

    datas = data.shape

    y0 = data[0, 0, :]
    x0 = np.arange(len(y0))
    # ratio to transfer energy value back to channel value
    approx_ratio = 100

    lowv = param['non_fitting_values']['energy_bound_low'] * approx_ratio
    highv = param['non_fitting_values']['energy_bound_high'] * approx_ratio
    x, y = trim(x0, y0, lowv, highv)
    start_i = x0[x0 == x[0]][0]
    end_i = x0[x0 == x[-1]][0]
    e_select, matv = construct_linear_model(x, param)
    mat_sum = np.sum(matv, axis=0)

    elist = param['non_fitting_values']['element_list'].split(', ')
    elist = [e.strip(' ') for e in elist]
    elist = [e+'_K' for e in elist if ('_' not in e)]

    result_pool = [pool.apply_async(fit_per_line,
                                    (i, data[:, :, start_i:end_i+1], matv, param)) for i in range(datas[0])]

    results = []
    for r in result_pool:
        results.append(r.get())

    pool.terminate()
    pool.join()

    # results = []
    # for i in range(datas[0]):
    #     outv = fit_per_line(i, data[:, :, start_i:end_i+1], matv, param)
    #     results.append(outv)

    results = np.array(results)

    non_element = ['compton', 'elastic', 'background']
    total_list = elist + non_element

    result_map = dict()
    for i in range(len(total_list)-1):
        result_map.update({total_list[i]: results[:, :, i]*mat_sum[i]})

    # add background
    result_map.update({total_list[-1]: results[:, :, -1]})

    # for v in total_list:
    #     for i in xrange(datas[0]):
    #         for j in xrange(datas[1]):
    #             result_map[v][i, j] = results[i, j].get(v, 0)

    sum_total = np.zeros([results.shape[0], results.shape[1], matv.shape[0]])
    for m in range(sum_total.shape[0]):
        for n in range(sum_total.shape[1]):
            for i in range(len(total_list)):
                sum_total[m, n, :] += results[m, n, i] * matv[:, i]

    print('label range: {}, {}'.format(start_i, end_i))
    #import pickle
    fit_path = '/Users/Li/Downloads/xrf_data/'
    fpath = os.path.join(fit_path, 'fit_data')
    #pickle.dump(result_map, open(fpath, 'wb'))
    np.save(fpath, sum_total)

    return result_map


# def fit_pixel_fast_multi(data, param):
#     """
#     Multiprocess fit of experiment data.
#
#     Parameters
#     ----------
#     data : array
#         3D data of experiment spectrum
#     param : dict
#         fitting parameters
#
#     Returns
#     -------
#     dict :
#         fitting values for all the elements
#     """
#
#     #logger.info('Row number at {} out of total {}'.format(i, datas[0]))
#     #logger.info('no_processors_to_use = {}'.format(no_processors_to_use))
#     no_processors_to_use = multiprocessing.cpu_count()
#     logger.info('cpu count: {}'.format(no_processors_to_use))
#     #print 'Creating pool with %d processes\n' % no_processors_to_use
#     pool = multiprocessing.Pool(no_processors_to_use)
#
#     datas = data.shape
#
#     x0 = np.arange(datas[2])
#
#     elist = param['non_fitting_values']['element_list'].split(', ')
#     elist = [e.strip(' ') for e in elist]
#     elist = [e+'_K' for e in elist if ('_' not in e)]
#
#     non_element = ['compton', 'elastic', 'background']
#     total_list = elist + non_element
#
#     result_map = dict()
#     for v in total_list:
#         result_map.update({v: np.zeros([datas[0], datas[1]])})
#
#     result_pool = [pool.apply_async(fit_per_line,
#                                     (i, data, param)) for i in range(datas[0])]
#
#     results = []
#     for r in result_pool:
#         results.append(r.get())
#
#     pool.terminate()
#     pool.join()
#
#     results = np.array(results)
#
#     for v in total_list:
#         for i in xrange(datas[0]):
#             for j in xrange(datas[1]):
#                 result_map[v][i, j] = results[i, j].get(v, 0)
#
#     return result_map


def fit_pixel(y, expected_matrix, constant_weight=10):
    """
    Non-negative linear fitting is applied for each pixel.

    Parameters
    ----------
    y : array
        spectrum of experiment data
    expected_matrix : array
        2D matrix of activated element spectrum
    constant_weight : float
        value used to calculate weight like so:
        weights = constant_weight / (constant_weight + spectrum)

    Returns
    -------
    results : array
        weights of different element
    residue : array
        error
    """
    if constant_weight:
        results, residue = weighted_nnls_fit(y, expected_matrix, constant_weight=constant_weight)
    else:
        results, residue = nnls_fit(y, expected_matrix)
    return results, residue


def fit_pixel_slow_version(data, param, c_val=1e-2, fit_num=10, c_weight=1):
    datas = data.shape

    x0 = np.arange(datas[2])

    elist = param['non_fitting_values']['element_list'].split(', ')
    elist = [e.strip(' ') for e in elist]

    result_map = dict()
    for v in elist:
        result_map.update({v: np.zeros([datas[0], datas[1]])})

    MS = ModelSpectrum(param)
    MS.model_spectrum()

    for i in xrange(datas[0]):
        logger.info('Row number at {} out of total {}'.format(i, datas[0]))
        for j in xrange(datas[1]):
            logger.info('Column number at {} out of total {}'.format(j, datas[1]))
            y0 = data[i, j, :]
            result = MS.model_fit(x0, y0,
                                  w=1/np.sqrt(c_weight+y0))
                                  #maxfev=fit_num, xtol=c_val, ftol=c_val, gtol=c_val)
            #for k, v in six.iteritems(result.values):
            #    print('result {}: {}'.format(k, v))
            # save result
            for v in elist:
                if '_L' in v:
                    line_name = v.split('_')[0]+'_la1_area'
                elif '_M' in v:
                    line_name = v.split('_')[0]+'_ma1_area'
                else:
                    line_name = v+'_ka1_area'
                result_map[v][i, j] = result.values[line_name]

    return result_map


def write_to_hdf(fpath, data_dict):
    """
    Add fitting results to existing h5 file. This is to be moved to filestore.

    Parameters
    ----------
    fpath : str
        path of the hdf5 file
    data_dict : dict
        dict of array
    """
    import h5py
    f = h5py.File(fpath, 'r+')
    det = 'det1'
    dataGrp = f['xrfmap/'+det]

    data = []
    namelist = []
    for k, v in six.iteritems(data_dict):
        namelist.append(str(k))
        data.append(v)

    if 'xrf_fit' in dataGrp:
        del dataGrp['xrf_fit']

    data = np.array(data)
    ds_data = dataGrp.create_dataset('xrf_fit', data=data)
    ds_data.attrs['comments'] = 'All fitting values are saved.'

    if 'xrf_fit_name' in dataGrp:
        del dataGrp['xrf_fit_name']

    name_data = dataGrp.create_dataset('xrf_fit_name', data=namelist)
    name_data.attrs['comments'] = 'All elements for fitting are saved.'

    f.close()


def compare_result(m, n, start_i=151, end_i=1350, all=True, linear=True):

    import h5py
    import matplotlib.pyplot as plt

    x = np.arange(end_i-start_i)

    fpath = '/Users/Li/Downloads/xrf_data/Root.h5'
    myfile = h5py.File(fpath, 'r')
    data_exp = myfile['xrfmap/det1/counts']

    fpath_fit = '/Users/Li/Downloads/xrf_data/fit_data.npy'
    d_fit = np.load(fpath_fit)

    if not all:
        if linear:
            plt.plot(x, data_exp[m, n, start_i:end_i], x, d_fit[m, n, :])
        else:
            plt.semilogy(x, data_exp[m, n, start_i:end_i], x, d_fit[m, n, :])
        plt.show()

    else:
        if linear:
            plt.plot(x, np.sum(data_exp[:,:, start_i:end_i], axis=(0, 1)), x, np.sum(d_fit, axis=(0, 1)))
        else:
            plt.semilogy(x, np.sum(data_exp[:,:, start_i:end_i], axis=(0, 1)), x, np.sum(d_fit, axis=(0, 1)))
        plt.show()
