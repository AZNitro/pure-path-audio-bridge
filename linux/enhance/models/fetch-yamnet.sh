#!/bin/bash
# Re-fetch the YAMNet model files and verify them against the hashes recorded
# in NOTICE. The files are Apache-2.0 and are redistributed in this repository,
# so this script is for reproducibility, not a required install step.
set -euo pipefail
cd "$(dirname "$0")"

MODEL_URL="https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite"
MAP_URL="https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"

MODEL_SHA=4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf
MAP_SHA=cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2

fetch() {   # url, dest, expected sha256
    echo "fetching $2"
    curl -fsSL "$1" -o "$2.tmp"
    got=$(sha256sum "$2.tmp" | cut -d' ' -f1)
    if [ "$got" != "$3" ]; then
        echo "SHA-256 mismatch for $2" >&2
        echo "  expected $3" >&2
        echo "  got      $got" >&2
        rm -f "$2.tmp"; exit 1
    fi
    mv "$2.tmp" "$2"
    echo "  ok, sha256 $got"
}

fetch "$MODEL_URL" yamnet.tflite         "$MODEL_SHA"
fetch "$MAP_URL"   yamnet_class_map.csv  "$MAP_SHA"
echo "done. See NOTICE for provenance and licence."
