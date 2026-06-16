"""Prompt registry.

Every prompt in corpusqa lives in this package as a (template, InputModel,
OutputModel) triple -- never as inline f-strings in logic modules. This is
the future DSPy insertion point: each entry is already signature-shaped
(typed input -> typed output), so adoption is a mechanical port if routing
accuracy ever plateaus under hand-tuning (design doc section 1.2).

Templates use ``str.format`` with named fields drawn from the input model.

M3-C1: prompt modules hold (InputModel, SYSTEM, TEMPLATE) only; output
models live with their pipeline stage and are referenced by the caller --
prompts are leaf data and must not import pipeline modules (cycle otherwise).
"""
