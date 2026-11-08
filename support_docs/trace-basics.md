# Reading a trace

Instrumentation records the operations in a request as spans. A span has a name,
a start time, an end time, attributes, and a status. Child spans share a trace ID
with their parent and have their own span IDs. The current context connects
nested operations. Inspect the timeline and error status before interpreting
latency. A model response still needs human review for usefulness and accuracy.
