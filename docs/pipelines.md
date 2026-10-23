# Adding a pipeline

Getting a workflow — one of your own, or something off GitHub — running as a Cirro
pipeline is three steps, and Claude will walk you through them in order:

1. **Read the workflow.** Claude parses the repository and writes down every input and
   parameter it takes: which are required, which have defaults, which are just plumbing
   Cirro supplies for you. You get a `design-spec.md` to correct — this is the step where
   your knowledge of the science matters most, because the parser can only report what
   the code says, not which of 60 parameters a user should actually see.
2. **Pick something to test it on.** A small dataset, the parameters to run it with, and
   the specific things that must be true for the run to count as working.
3. **Configure and register it.** Claude generates the form your colleagues will fill in,
   the mapping from that form to the workflow's parameters, and the compute settings —
   then registers the pipeline.

## Where the configuration lives

You choose: uploaded into Cirro, kept in the workflow's own repository, or kept in a
separate configuration repository. If it lives in a repository, Claude writes and
commits the files and **you** push them — it does not push, and it does not open pull
requests.

Either way you keep the files. Uploading into Cirro also saves a copy in a
`pipeline-config/` folder next to the design spec, so months later you can read what a
pipeline is actually configured to do without asking Cirro for it back.

## Writing the configuration yourself

Generating it from a spec is the quick path, but some forms cannot be described in one —
a field revealed by two different selectors, a custom widget, an input map tuned by
hand. Point Claude at a folder of `process-form.json` and friends instead of a spec and
those files go up exactly as written: validated, never reformatted, never regenerated.
No copy is staged in that case, because the files are already yours and already where
you put them.

## Worth knowing

- **Claude only edits pipelines you own.** One belonging to a colleague, or curated by
  Cirro, is refused rather than changed.
- **Configuration is checked before it goes anywhere**, whether generated or written by
  hand. `cirro-agent read check-config` catches the failures the portal reports
  unhelpfully or not at all — form fields the renderer will ignore, file pickers that
  will show an empty list, parameters wired to fields that do not exist. Nothing in that
  check can tell you whether the form makes sense to a biologist, so open it in the
  Cirro UI once before sharing it.
- **Registering a pipeline has not yet been done against a live tenant.** The generated
  output is checked against the configuration of 213 pipelines already running in Cirro,
  but treat your first registration as untested — see [Status](status.md).
