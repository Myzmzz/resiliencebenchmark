"""Provision replicas of the system under test and run trials on them in parallel.

The Fleet service owns three things and nothing else: provisioning replica
namespaces with one Controller instance each, dispatching a batch of trials to
those Controllers, and collecting the results. Trial semantics -- prompts,
scoring, disturbances, cleanup -- stay entirely inside the Controller.
"""
