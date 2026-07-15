# Repository guidance

This repository contains Chinese Kubernetes source-code lessons for an experienced platform/SRE operator who is extending into GPU operations and is still building Go-reading fluency.

## Content rules

- Keep `07_正式学习讲义/README.md` as the single authoritative lesson order.
- Do not turn lessons into product manuals or command dumps. Start from a production symptom, explain the design constraint and invariant, then enter the verified source path.
- Platform chapters use familiar Java workloads as the main case and keep GPU content to a short transfer. GPU-specific chapters may use GPU incidents as the main case.
- Explain Go syntax exactly where it blocks understanding. Add plain-language summaries after important source excerpts.
- Preserve the distinction between material completion and the learner's actual progress. Update `PROGRESS.md` only from real reading, self-test, or experiment results.

## Source verification

- Kubernetes source is intentionally not vendored in this repository.
- Before changing a source lesson, check out the named upstream commit or the target production version separately and verify paths, symbols, branches, errors, events, defaults, and line numbers.
- Treat `301946d15e67a4a2e8a5fb8292eb836acd366d78` as the current main teaching baseline, not as a universal production version.
- Keep third-party attribution consistent with `THIRD_PARTY_NOTICES.md`.

## Validation

- Check Markdown fences, relative links, navigation status, TODO/FIXME markers, encoding, and placeholders before committing.
- Do not claim that a command, test, or GPU experiment ran unless it actually ran.
- Never commit tokens, kubeconfigs, private keys, internal production identifiers, or unredacted incident data.
