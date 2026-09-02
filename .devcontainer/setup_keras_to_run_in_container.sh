#!/bin/bash
set -e

pip install -r /pre_version/keras/requirements.txt
pip install coverage

# Real dual-package-version mechanism: build genuine pre_keras / post_keras
# renamed distributions from the two checkouts and install both into this
# single container (keras is pure Python, but a plain "import keras" would
# still always resolve to whichever version was installed last -- see
# rename_keras_in_container.sh for why). See rename_keras_in_container.sh,
# ported from rename_scripts/keras.sh.
/root/rename_keras_in_container.sh
