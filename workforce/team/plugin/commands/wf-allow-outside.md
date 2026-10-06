---
description: Let Claude write outside the project folder without asking. Alone it allows everything for this session; with a path (/wf-allow-outside ~/Desktop/reports) only that file or folder; "off" or typing it alone again turns it off. Credential files stay off limits
argument-hint: "[file or folder ...] | off"
---

The WorkForce hook has already handled this command when you were prompted: it turned writing outside the project folder on or off for this session, or allowed the paths given. Report the hook's message to the user in one line, then carry on with whatever the user was doing.
