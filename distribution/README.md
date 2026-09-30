# Facebook Group Distribution Recommender

Phase 1 is recommendation-only. It never joins a Group, submits a post, uses browser
automation, or calls a Group-posting API. The operator manually shares the original,
already-published Facebook Page post and may record `SUBMITTED_PENDING_ADMIN` afterward.

The daily planner selects no more than three verified posts and uses a different approved
Group for each. Groups must be manually registered, recently reviewed, relevant by both
geography and topic, permit news links and Page content, and report at least 100,000 members.

Run the controlled preview without writing a ledger:

```bash
python distribution_cli.py --story distribution/controlled_test_story.json --preview
```

Both `GROUP_RECOMMENDER_ENABLED` and `GROUP_AUTO_SHARE_ENABLED` default to `false`.
Automatic Group posting is permanently disabled in Phase 1.
