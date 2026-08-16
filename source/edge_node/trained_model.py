"""AUTO-GENERATED — do not hand-edit. Regenerate via edge_node.train --export.

model_version: real-trial-171010rows-seed0
generated_at:  2026-08-16T08:01:27.965264+00:00
sklearn_version_at_export: 1.9.0
training_rows: 136808
features (in order): ['rsmi', 'light_delta', 'gas_index_trend', 'rsmi_trend']
classes: ['moisture_low', 'stable']

No runtime dependency on scikit-learn/numpy — this is a plain nested
if/else translation of the fitted tree, safe to run standalone on the
QRB2210 (architecture doc Section 9).
"""

MODEL_VERSION = 'real-trial-171010rows-seed0'

def trained_predict(rsmi, light_delta, gas_index_trend, rsmi_trend):
    if rsmi <= 0.995850:
        if gas_index_trend <= -1.337650:
            if rsmi <= 0.890150:
                if rsmi <= 0.857100:
                    if gas_index_trend <= -2.365200:
                        return 'stable', 1.0000
                    else:
                        return 'moisture_low', 1.0000
                else:
                    if gas_index_trend <= -3.686950:
                        return 'moisture_low', 0.6471
                    else:
                        return 'stable', 0.9686
            else:
                return 'moisture_low', 1.0000
        else:
            if gas_index_trend <= 1.020000:
                if rsmi <= 0.873500:
                    if rsmi <= 0.861150:
                        return 'moisture_low', 0.9980
                    else:
                        return 'stable', 0.5220
                else:
                    return 'moisture_low', 1.0000
            else:
                if rsmi <= 0.861150:
                    if gas_index_trend <= 1.675000:
                        return 'moisture_low', 0.9910
                    else:
                        return 'moisture_low', 0.6000
                else:
                    if rsmi <= 0.877650:
                        return 'stable', 0.6703
                    else:
                        return 'moisture_low', 1.0000
    else:
        if gas_index_trend <= 0.708750:
            if gas_index_trend <= -0.644800:
                if gas_index_trend <= -0.949800:
                    if rsmi_trend <= 0.007100:
                        return 'stable', 0.9947
                    else:
                        return 'moisture_low', 0.5000
                else:
                    if gas_index_trend <= -0.753050:
                        return 'stable', 0.9531
                    else:
                        return 'stable', 0.9238
            else:
                if gas_index_trend <= 0.534800:
                    if gas_index_trend <= -0.479450:
                        return 'stable', 0.8900
                    else:
                        return 'stable', 0.8323
                else:
                    if gas_index_trend <= 0.619100:
                        return 'stable', 0.8755
                    else:
                        return 'stable', 0.9105
        else:
            if gas_index_trend <= 1.032300:
                if gas_index_trend <= 1.031950:
                    if gas_index_trend <= 0.926250:
                        return 'stable', 0.9533
                    else:
                        return 'stable', 0.9708
                else:
                    return 'stable', 0.7778
            else:
                if gas_index_trend <= 3.892500:
                    if gas_index_trend <= 1.115350:
                        return 'stable', 0.9806
                    else:
                        return 'stable', 0.9929
                else:
                    if gas_index_trend <= 3.900550:
                        return 'moisture_low', 1.0000
                    else:
                        return 'stable', 0.9603
