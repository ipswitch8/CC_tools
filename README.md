A couple of commands to:\
/a = use agents in a project (will also auto-plan and pipeline phased work with Karen added between phases)\
/p = similar to /a but forces planning and pipeline process instead of deciding on the fly\
/k = a specific agent used to verify Claude's most recent work in a project (i.e. after /a)\
/g = submit changes to the project Git repo (i.e. after /k, or maybe both before and after depending on the amount of work done!)\
\
How to use - put these folders in your ~/.claude/ folder and merge the CLAUDE.md and settings.json with your existing ones.
\
Q: What is the "agents" folder\
A: Agents for Claude\
\
Q: What is the "commands" folder?\
A: Commands for Claude, described above.\
\
Q: What is the "hooks" folder?\
A: Scripts to support the pipeline process.\
\
Q: What is the pipeline process?\
A: Basically "yolo mode with guardrails", everything that doesn't involve Internet access is approved, and complex work with phases has sanity-checking between the phases.\
\
Q: What is the docs folder?\
A: Just one doc at the moment, mostly about changes in the agent registry that could affect other commands if you grabbed v1 of this repo.\
\
Q: What is the "CC_multi" folder?\
A1: A small script to more easily manage dozens of simultaneous Claude Code CLI sessions scattered across multiple projects on multiple servers and fully utilize multiple screens, with various features for session management, reconnection, etc.\
A2: Read the README.md file in that folder for more info.
