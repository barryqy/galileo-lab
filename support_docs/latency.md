# Investigating a slow assistant

Start with total request latency, model duration, token usage, and errors.
A trace timeline shows how the request spends its time. A slow model call and
a slow document lookup need different fixes. Compare several real requests
before blaming one component. Missing provider usage means unavailable, not zero.
