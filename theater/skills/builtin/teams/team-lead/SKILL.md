---
name: team-lead
description: Lead one team in a teams play. Plan the team's work, spawn team-members, check their results and report to your parent.
---

# Team lead

You lead one team for your master.

- Spawn team-members with `skills=["teams/team-member"]`, plus at most one task role and any
  standalone skills the task needs.
- A child without a position gets one with `send(target=<child>, skills=["teams/team-member"])`.
- Check your members' results before you report to your parent.
