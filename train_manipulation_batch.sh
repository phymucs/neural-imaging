#!/usr/bin/bash

# If working on the server, load dependencies
if [ "$USER" = "pk91" ]; then
    module load cudnn/9.0v7.3.0.29
    source ../neural-imaging-pipeline/venv/bin/activate
fi

if [ -n "$2" ]; then
  rep=$2
else
  rep=5
fi

dir="7m-no-pool"
nip="DNet"
cam="D90"
manip="sharpen,gaussian,jpeg,resample,awgn,median"
cmd="python3 train_manipulation.py --end $rep --patch 128 --epochs=2501 --ds none --nip $nip --cam $cam --manip $manip"

if [ "$2" = "dry" ]; then
    cmd="echo $cmd"
fi

# Scope of parameters for exploration
ln="--ln 0.5 --ln 0.4 --ln 0.3 --ln 0.2 --ln 0.1 --ln 0.075 --ln 0.05 --ln 0.025 --ln 0.01 --ln 0.005 --ln 0.001"
lc="--lc 1.0 --lc 0.5 --lc 0.1 --lc 0.05 --lc 0.01 --lc 0.005 --lc 0.001"

case "$1" in
    jpeg)
        # Fixed JPEG Experiments
        for jpeg in 50 90 70 80 60 40 30 20 10 55 65 75 85 95 45 35 25 15; do
            $cmd --dir ./data/raw/$dir/jpeg/$jpeg --jpeg $jpeg
        done
        ;;
   jpeg+nip)
        # Fixed JPEG + Trainable NIP
        for jpeg in 30 40 50 60 70; do
            $cmd --dir ./data/raw/$dir/jpeg-nip+/$jpeg --jpeg $jpeg --train nip $ln
        done
        ;;
    dcn)
        # Fixed DCN Experiments
        for dcn in 4k 8k 16k; do
            $cmd --dir ./data/raw/$dir/dcn/$dcn --dcn $dcn
        done
        ;;
    dcn+nip)
        # DCN + Trainable NIP
        for dcn in 4k 8k 16k; do
            $cmd --dir ./data/raw/$dir/dcn-nip+/$dcn --dcn $dcn --train nip $ln
        done
       ;;
   dcn+)
        # Trainable DCN
        for dcn in 4k 8k 16k; do
            $cmd --dir ./data/raw/$dir/dcn+/$dcn --dcn $dcn --train dcn $lc
        done
        ;;
   *)
        echo $"Usage: $0 {jpeg|dcn|dcn+nip|dcn+|jpeg+nip} rep [down]"
        exit 1
esac
