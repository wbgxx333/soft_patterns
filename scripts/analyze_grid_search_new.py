#!/usr/bin/env python

import sys
import re
import os.path
import glob
import numpy as np

home = os.environ.get('HOME')
workdir=home+"/work/soft_patterns/logs/"

def main(args):
    type = 0
    if len(args) < 3:
        print("Usage:",args[0],"<prefix> <param file> <type (0 for accuracy [default], 1 for loss)>")
        return -1
    elif len(args) > 3:
        type = int(args[3])

    prefix = args[1]
    param_file = args[2]

    s=workdir + prefix + '*.out'
    files = glob.glob(s)

    if len(files) == 0:
        print("No files found for", prefix)
        return -2

    params = get_params(param_file)

    global_best = None
    global_best_val = -1 if type == 0 else 1000

    for f in files:
        best = get_top(f, type)

        if best != -1:
            local_params = get_local_params(params, f, best)

            if best > global_best_val and type == 0 or (best < global_best_val and type == 1):
                global_best = f
                global_best_val = best

    analyze(local_params, type)

    print("Overall best: {} ({})".format(global_best_val, global_best))
    return 0

def get_params(param_file):
    with open(param_file) as ifh:
        params = [x.split() for x in ifh]

    filtered_params = dict()
    for p in params:
        if len(p) > 1:
            filtered_params[p[0]] = dict([(x,[]) for x in p[1:]])

    return filtered_params

def get_local_params(params, f, v):
    with open(f) as ifh:
        l = ifh.getline()

    vs = l[10:-1].split()

    for x in vs:
        e = x.split('=')

        if e[0] in params:
            params[e[0]][e[1]].append(v)

def analyze(local_params, type):
    for name in local_params:
        print(name+":")

        for k,v in local_params[name].items():
            if len(len(v)):
                print("\t{}: {}: {:,.3f}, Mean: {:,.3f} {}".format(k, "Max" if type == 0 else "Min", np.max(v) if type == 0 else np.min(v), np.mean(v), len(v)))
            else:
                print("\t",k,"No files")

def get_top(f, type):
    maxv = -1 if type == 0 else 1000

    with open(f) as ifh:
        for l in ifh:
            if l.find('dev loss:') != -1:
                e = l.rstrip().split()

                if type == 0:
                    acc = float(e[-1][:-1])
                    if acc > maxv:
                        maxv = acc
                else:
                    loss = float(e[-3])
                    if loss < maxv:
                        maxv = loss


    return maxv


if __name__ == '__main__':
    sys.exit(main(sys.argv))