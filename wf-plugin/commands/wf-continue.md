---
description: Override the usage stop (60%) for the current usage window and carry on
---

The WorkForce hook has already handled this command when you were prompted: it records an override for every usage window that is over the stop limit, valid until that window resets. Report the hook's message to the user in one line (which window, until when), then carry on with whatever the user was doing. If the hook says nothing was over the limit, say that.
