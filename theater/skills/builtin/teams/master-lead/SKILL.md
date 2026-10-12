---
name: master-lead
description: Lead a teams play as its master. Loading this skill starts the play with you as master, so load it only when the user chooses you; attach it on a spawn to start a dedicated master instead.
---

# Master lead

You lead this teams play. While you live, every agent started below you must hold a position.

- Spawn team-leads with `skills=["teams/team-lead"]`, plus at most one task role
  (`teams/worker`, `teams/reviewer` or `teams/debater`) and any standalone skills the task needs.
- A child you started before the play has no position yet; give it one with
  `send(target=<child>, skills=["teams/team-lead"])`.
- Give each lead a goal, the scope it owns and the result you expect; verify and integrate
  their results yourself.

The play ends when you do, and Theater then lifts its rules.
