"""The governed pipeline (0.6, test version; agent.pipeline = governed).

The classic agent lets the model write SQL and checks it afterwards. Here the model never writes a
query: the decider chooses the knowledge a question needs (decider, gate), the model fills a typed plan
from it (plan), code checks every part of the plan against what was said (validate), builds and runs the
queries (compile), and writes the interpretation of the answer (compose). Anything a plan cannot say
goes to the classic agent, labelled as such.

The core is plain Python; LangGraph only wires the steps (graph), so the core stays testable without it.
"""
