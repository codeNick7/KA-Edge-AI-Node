"""Runtime-only copy of source/edge_node's inference pipeline (features.py,
validity.py, trained_model.py, texture_table.py -- no train.py/export.py/
decision_tree.py, not needed at runtime).

Canonical source is source/edge_node/ -- this copy exists because only
KA-Edge-source/python/ gets deployed to the physical board (App Lab
project: /home/arduino/ArduinoApps/ka-edge-node/, see main.py's own
deployment note), so main.py's live push wiring needs these files
sitting alongside it, not one repo level up.

Re-sync manually after any change to source/edge_node/{features,
validity,trained_model,texture_table}.py -- no build step keeps these
in sync automatically.
"""
