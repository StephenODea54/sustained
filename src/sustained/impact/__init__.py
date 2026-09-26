"""
Statement impact analysis: what a migration statement does to a live
database while it runs, such as the locks it takes, what those locks
block, and whether the table is rewritten.

The package is being built in phases. So far it holds the shared
tokenizer (`sustained.impact.tokens`), the report model
(`sustained.impact.model`), and the recognizer that reads a statement's
text into a shape (`sustained.impact.shapes`).
"""
