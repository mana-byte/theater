---
name: master-lead
description: Lead a teams play as its master. Loading this skill starts the play with you as master, so load it only when the user chooses you; attach it on a spawn to start a dedicated master instead.
---

# Master lead

You lead this teams play. While you live, every agent started below you must hold a position.

The only position you may give a child is `teams/team-lead`. The task roles that fit a team-lead
are `teams/worker`, `teams/reviewer` and `teams/debater`. `whoami` lists the same choices under
`play.spawn_options`.

- Spawn team-leads with `skills=["teams/team-lead"]`, plus at most one of those task roles and any
  standalone skills the task needs.
- A child you started before the play has no position yet; give it one with
  `send(target=<child>, skills=["teams/team-lead"])`.
- Give each lead a goal, the scope it owns and the result you expect; verify and integrate
  their results yourself.

The play ends when you do, and Theater then lifts its rules.
