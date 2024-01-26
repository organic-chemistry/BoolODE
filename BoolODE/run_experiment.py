#!/usr/bin/env python
# coding: utf-8
__author__ = 'Amogh Jalihal'
import os
import sys
import ast
import time
import warnings
import numpy as np
import pandas as pd
from tqdm import tqdm
from pathlib import Path
from optparse import OptionParser
from itertools import combinations
from scipy.integrate import odeint
from sklearn.cluster import KMeans
import copy
from importlib.machinery import SourceFileLoader
import multiprocessing as mp
# local imports
from BoolODE import utils
from BoolODE.model_generator import GenerateModel
from BoolODE import simulator 

np.seterr(all='raise')

def Experiment(mg, Model,
               tspan,
               settings,
               icsDF,
               writeProtein=False,
               normalizeTrajectory=False,init_seed=0):
    """
    Carry out an `in-silico` experiment. This function takes as input 
    an ODE model defined as a python function and carries out stochastic
    simulations. BoolODE defines a _cell_ as a single time point from 
    a simulated time course. Thus, in order to obtain 50 single cells,
    BoolODE carries out 50 simulations, which are stored in ./simulations/.
    Further, if it is known that the resulting single cell dataset will
    exhibit multiple trajectories, the user can specify  the number of clusters in
    `nClusters`; BoolODE will then cluster the entire simulation, such that each
    simulated trajectory possesess a cluster ID.

    :param mg: Model details obtained by instantiating an object of GenerateModel
    :type mg: BoolODE.GenerateModel
    :param Model: Function defining ODE model
    :type Model: function
    :param tspan: Array of time points
    :type tspan: ndarray
    :param settings: The job settings dictionary
    :type settings: dict
    :param icsDF: Dataframe specifying initial condition for simulation
    :type icsDF: pandas DataFrame or list of initial_configurations
    :param writeProtein: Bool specifying if the protein values should be written to file. Default = False
    :type writeProtein: bool
    :param normalizeTrajectory: Bool specifying if the gene expression values should be scaled between 0 and 1.
    :type normalizeTrajectory: bool 
    """
    ####################    
    allParameters = dict(mg.ModelSpec['pars'])
    parNames = sorted(list(allParameters.keys()))
    ## Use default parameters 
    pars = [mg.ModelSpec['pars'][k] for k in parNames]
    ####################
    rnaIndex = [i for i in range(len(mg.varmapper.keys())) if 'x_' in mg.varmapper[i]]
    revvarmapper = {v:k for k,v in mg.varmapper.items()}
    proteinIndex = [i for i in range(len(mg.varmapper.keys())) if 'p_' in mg.varmapper[i]]

    y0 = [mg.ModelSpec['ics'][mg.varmapper[i]] for i in range(len(mg.varmapper.keys()))]
    ss = np.zeros(len(mg.varmapper.keys()))
    
    for i,k in mg.varmapper.items():
        if 'x_' in k:
            ss[i] = 1.0
        elif 'p_' in k:
            if k.replace('p_','') in mg.proteinlist:
                # Seting them to the threshold
                # causes them to drop to 0 rapidly
                # TODO: try setting to threshold < v < y_max
                ss[i] = 20.
    multi_ss = False     
    if type(icsDF) != list and not icsDF.empty:
        icsspec = icsDF.loc[0]
        genes = ast.literal_eval(icsspec['Genes'])
        values = ast.literal_eval(icsspec['Values'])
        icsmap = {g:v for g,v in zip(genes,values)}
        for i,k in mg.varmapper.items():
            for p in mg.proteinlist:
                if p in icsmap.keys():
                    ss[revvarmapper['p_'+p]] = icsmap[p]
                else:
                    ss[revvarmapper['p_'+p]] = 0.01
            for g in mg.genelist:
                if g in icsmap.keys():
                    ss[revvarmapper['x_'+g]] = icsmap[g]
                else:
                    ss[revvarmapper['x_'+g]] = 0.01
    else:
        #Multiple initial starting configuration
        multi_ss=True
        ss = icsDF[0] # just for initialising ss

            
    if len(mg.proteinlist) == 0:
        result = pd.DataFrame(index=pd.Index([mg.varmapper[i] for i in rnaIndex]))
    else:
        speciesoi = [revvarmapper['p_' + p] for p in proteinlist]
        speciesoi.extend([revvarmapper['x_' + g] for g in mg.genelist])
        result = pd.DataFrame(index=pd.Index([mg.varmapper[i] for i in speciesoi]))
        
    # Index of every possible time point. Sample from this list
    startat = 0
    timeIndex = [i for i in range(startat, len(tspan))]        

    ## Construct dictionary of arguments to be passed
    ## to simulateAndSample(), done in parallel
    outPrefix = str(settings['outprefix'])
    argdict = {}
    argdict['mg'] = mg
    argdict['allParameters'] = allParameters
    argdict['parNames'] = parNames
    argdict['Model'] = Model
    argdict['tspan'] = tspan
    argdict['varmapper'] = mg.varmapper
    argdict['timeIndex'] = timeIndex
    argdict['genelist'] = mg.genelist
    argdict['proteinlist'] = mg.proteinlist
    argdict['writeProtein'] = writeProtein
    argdict['outPrefix'] = outPrefix
    argdict['sampleCells'] = settings['sample_cells'] # TODO consider removing this option
    argdict['pars'] = pars
    argdict['ss'] = ss
    argdict['ModelSpec'] = mg.ModelSpec
    argdict['rnaIndex'] = rnaIndex
    argdict['proteinIndex'] = proteinIndex
    argdict['revvarmapper'] = revvarmapper
    argdict['x_max'] = mg.kineticParameterDefaults['x_max']
    if multi_ss:
        argdict['get_prot'] = False
    else :
        argdict['get_prot'] = True

    if settings['sample_cells']:
        # pre-define the time points from which a cell will be sampled
        # per simulation
        sampleAt = np.random.choice(timeIndex, size=settings['num_cells'])
        header = ['E' + str(cellid) + '_' + str(time) \
                  for cellid, time in\
                  zip(range(settings['num_cells']), sampleAt)]
        
        argdict['header'] = header
    else:
        # initialize dictionary to hold raveled values, used to cluster
        # This will be useful later.
        groupedDict = {}         

    simfilepath = Path(outPrefix, './simulations/')
    if not os.path.exists(simfilepath):
        print(simfilepath, "does not exist, creating it...")
        os.makedirs(simfilepath)
    print('Starting simulations')
    start = time.time()

    states = []

    if settings['doParallel']:
        with mp.Pool() as pool:
            jobs = []
            for cellid in range(settings['num_cells']):

                if multi_ss:
                    argdict["ss"] = icsDF[cellid]

                cell_args = dict(argdict, seed=cellid + init_seed, cellid=cellid)
                job = pool.apply_async(simulateAndSample, args=(cell_args,))
                jobs.append(job)
                
            states = [job.get() for job in jobs]
    else:
        for cellid in tqdm(range(settings['num_cells'])):
            argdict['seed'] = cellid + init_seed
            argdict['cellid'] = cellid
            if multi_ss:
                argdict["ss"] = icsDF[cellid]

            states.append(simulateAndSample(argdict))

    # extract mean time and final_states
    final_states=[]
    avg_traj = []
    n_traj = len(states)
    for f,whole_t in states:
        final_states.append(f)
        if len(avg_traj) != 0 :
            avg_traj += whole_t[::2,:] / n_traj
        else:
            avg_traj  = whole_t[::2,:].copy() / n_traj


    print("Simulations took %0.3f s"%(time.time() - start))
    frames = []
    print('starting to concat files')
    start = time.time()

    for cellid in tqdm(range(settings['num_cells'])):
        if settings['sample_cells']:
            df = pd.read_csv(outPrefix + '/simulations/E'+str(cellid) + '-cell.csv',index_col=0)
            df = df.sort_index()                
        else:
            df = pd.read_csv(outPrefix + '/simulations/E'+str(cellid) + '.csv',index_col=0)
            df = df.sort_index()
            groupedDict['E' + str(cellid)] = df.values.ravel()
        frames.append(df.T)
    stop = time.time()
    print("Concating files took %.2f s" %(stop-start))
    result = pd.concat(frames,axis=0)
    result = result.T
    indices = result.index
    newindices = [i.replace('x_','') for i in indices]
    result.index = pd.Index(newindices)
    
    if settings['nClusters'] > 1:
        ## Carry out k-means clustering to identify which
        ## trajectory a simulation belongs to
        print('Starting k-means clustering')
        groupedDF = pd.DataFrame.from_dict(groupedDict)
        print('Clustering simulations...')
        start = time.time()            
        # Find clusters in the experiments
        clusterLabels= KMeans(n_clusters=settings['nClusters'],
                              n_jobs=8).fit(groupedDF.T.values).labels_
        print('Clustering took %0.3fs' % (time.time() - start))
        clusterDF = pd.DataFrame(data=clusterLabels, index =\
                                 groupedDF.columns, columns=['cl'])
        clusterDF.to_csv(outPrefix + '/ClusterIds.csv')
    else:
        print('Requested nClusters=1, not performing k-means clustering')
    ##################################################
    
    return result,final_states,avg_traj
    
def startRun(settings):
    """
    Start a simulation run. Loads model file, starts an Experiment(),
    and generates the appropriate input files
    """
    validInput = utils.checkValidModelDefinitionPath(settings['modelpath'], settings['name'])
    startfull = time.time()

    outdir = settings['outprefix']
    if not os.path.exists(outdir):
        print(outdir, "does not exist, creating it...")
        os.makedirs(outdir)
        
    ##########################################
    ## Read advanced model specification files
    ## If these are not specified, the dataFrame objects
    ## are left empty
    parameterInputsDF = utils.checkValidInputPath(settings['parameter_inputs_path'])
    parameterSetDF = utils.checkValidInputPath(settings['parameter_set'])
    icsDF = utils.checkValidInputPath(settings['icsPath'])
    interactionStrengthDF = utils.checkValidInputPath(settings['interaction_strengths'])

    speciesTypeDF = utils.checkValidInputPath(settings['species_type'])
    ##########################################

    # Simulator settings
    tmax = settings['simulation_time']    
    integration_step_size = settings['integration_step_size']
    tspan = np.linspace(0,tmax,int(tmax/integration_step_size))

    # Generate the ODE model from the specified boolean model
    mg = GenerateModel(settings,
                       parameterInputsDF,
                       parameterSetDF,
                       interactionStrengthDF)
    
  
 
    genesDict = {}

    # Load the ODE model file
    print("Model",mg.path_to_ode_model.as_posix())
    model = SourceFileLoader("model", mg.path_to_ode_model.as_posix()).load_module()

    ## Function call - do the in silico experiment
    resultDF,final_states,avg_traj = Experiment(mg, model.Model,
                          tspan,
                          settings,
                          icsDF,
                          writeProtein=settings['writeProtein'],
                          normalizeTrajectory=settings['normalizeTrajectory'])
    
    # Write simulation output. Creates ground truth files.
    print('Generating input files for pipline...')
    start = time.time()
    utils.generateInputFiles(resultDF, mg.df,
                             mg.withoutRules,
                             parameterInputsDF,
                             tmax,
                             settings['num_cells'],
                             outPrefix=settings['outprefix'])
    print('Input file generation took %0.2f s' % (time.time() - start))
    print("BoolODE.py took %0.2fs"% (time.time() - startfull))
    return {"mg":mg,"model":model,"tspan":tspan,"final_states":final_states,"avg_traj":avg_traj}

def startPerturbations(settings,previous_run={},perturbation_level=2,single=True):
    """
    from the previous cells, run perturbation experiment
    """
    ###############
    # Generate the list of perturbation
    print("########################")
    print("startPerturbations")
  
    apply_perturbation = lambda x : x  * perturbation_level


    mg = previous_run["mg"]
    gid = [int(n.replace("x_g","")) for i,n in mg.varmapper.items() if 'x_' in n] # list tof gene_id
    print("Genes id",gid)


    if single:    
        list_perturbations = [[i] for i in gid]
    else:
        list_perturbations = []
        for i,p1 in enumerate(gid):
            for p2 in gid[i+1:]:
                list_perturbations.append([p1,p2])

    print("list of perturbation", list_perturbations)
    allParameters = dict(mg.ModelSpec['pars'])
    parNames = sorted(list(allParameters.keys()))
    ## Use default parameters 
    pars = [mg.ModelSpec['pars'][k] for k in parNames]


    #Initial parameters 
    init_pars = copy.deepcopy(pars)
    init_out_prefix = copy.deepcopy(settings['outprefix'])
    #print(len(previous_run["final_states"]))
    if single:
        lab = [0]
    else:
        lab=[0,0]
    final_states={str(lab):previous_run["final_states"]}
    avg_trajs={}
    #print(mg.ModelSpec['pars'])
    for perturbations in list_perturbations:
        for gene in perturbations:
            for par_name in parNames:
                if par_name == "m_g%i"%gene:
                    print("Init",mg.ModelSpec['pars'][par_name] )
                    mg.ModelSpec['pars'][par_name] = apply_perturbation(mg.ModelSpec['pars'][par_name]) 
                    print("After",mg.ModelSpec['pars'][par_name] )
        
        settings['outprefix'] = str(init_out_prefix) + "/Perturbation_" + "_".join(map(str,perturbations))

        resultDF,final_state,avg_traj = Experiment(mg, 
                              previous_run["model"].Model,
                              previous_run["tspan"],
                              settings,
                              previous_run["final_states"],
                              writeProtein=settings['writeProtein'],
                               normalizeTrajectory=settings['normalizeTrajectory'],init_seed=1)
        final_states[str(perturbations)]=final_state
        avg_trajs[ str(init_out_prefix) +"/dynamics/Perturbation_" + "_".join(map(str,perturbations))+".png"] = avg_traj
        for i,k in enumerate(parNames):
            mg.ModelSpec['pars'][k] = init_pars[i]


    settings['outprefix']  = init_out_prefix
    #print(final_states)
    return {"final_states":final_states,"gid":gid,"avg_trajs":avg_trajs}


def simulateAndSample(argdict):
    """
    Handles parallelization of ODE simulations.
    Calls the simulator with simulation settings.
    """
    mg = argdict['mg']
    allParameters = argdict['allParameters']
    parNames = argdict['parNames']
    Model = argdict['Model']
    tspan = argdict['tspan']
    varmapper = argdict['varmapper']
    timeIndex = argdict['timeIndex']
    genelist = argdict['genelist']
    proteinlist = argdict['proteinlist']
    writeProtein=argdict['writeProtein']
    cellid = argdict['cellid']
    outPrefix = argdict['outPrefix']
    sampleCells = argdict['sampleCells']
    ss = argdict['ss']
    ModelSpec = argdict['ModelSpec']
    rnaIndex = argdict['rnaIndex']
    proteinIndex = argdict['proteinIndex']
    genelist = argdict['genelist']
    proteinlist = argdict['proteinlist']
    revvarmapper = argdict['revvarmapper']
    seed = argdict['seed']
    pars = argdict['pars']
    x_max = argdict['x_max']
    get_prot = argdict["get_prot"]
    
    # Retained for debugging
    isStochastic = True
    
    if sampleCells:
        header = argdict['header']
        
    #print(allParameters)
    pars = {}
    for k, v in allParameters.items():
        pars[k] = v
    #print(pars)
    #print(parNames)
    pars = [pars[k] for k in parNames]
    
    ## Boolean to check if a simulation is going to a
    ## 0 steady state, with all genes/proteins dying out
    retry = True
    trys = 0
    ## timepoints
    tps = [i for i in range(1,len(tspan))]
    ## gene ids
    gid = [i for i,n in varmapper.items() if 'x_' in n]
    alls = [i for i,n in varmapper.items() ]
    outPrefix = outPrefix + '/simulations/'
    while retry:
        seed += 1000
        #print("Init out",ss)
        #print(parNames)
        if get_prot:
            y0_exp = simulator.getInitialCondition(ss, ModelSpec, rnaIndex, proteinIndex,
                                     genelist, proteinlist,
                                     varmapper,revvarmapper)
        else:
            y0_exp = ss
        #print(y0_exp)
        #raise
        P = simulator.simulateModel(Model, y0_exp, pars, isStochastic, tspan, seed)
        P = P.T
        retry = False
        ## Extract Time points
        subset = P[gid,:][:,tps]
        df = pd.DataFrame(subset,
                          index=pd.Index(genelist),
                          columns = ['E' + str(cellid) +'_' +str(i)\
                                     for i in tps])
        df.to_csv(outPrefix + 'E' + str(cellid) + '.csv')        
        ## Heuristic:
        ## If the largest value of a protein achieved in a simulation is
        ## less than 10% of the y_max, drop the simulation.
        ## This check stems from the observation that in some simulations,
        ## all genes go to the 0 steady state in some rare simulations.
        dfmax = df.max()
        for col in df.columns:
            colmax = df[col].max()
            if colmax < 0.1*x_max:
                retry= True
                break
        
        if sampleCells:
            ## Write a single cell to file
            ## These samples allow for quickly and
            ## reproducibly testing the output.
            sampledf = utils.sampleCellFromTraj(cellid,
                                          tspan, 
                                          P,
                                          varmapper, timeIndex,
                                          genelist, proteinlist,
                                          header,
                                          writeProtein=writeProtein)
            sampledf = sampledf.T
            sampledf.to_csv(outPrefix + 'E' + str(cellid) + '-cell.csv')            
            
        trys += 1
        # write to file
        df.to_csv(outPrefix + 'E' + str(cellid) + '.csv')
        
        if trys > 1:
            print('try', trys)


    return P[alls,:][:,tps[-1]],P[alls,:][:,:]
