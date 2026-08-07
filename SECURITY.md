# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository: open the
**Security** tab and choose **Report a vulnerability**. That channel is private
between you and me. Please do not open a public issue for a security problem.

I read these. Expect a first reply within a week.

## What is in scope

Anything that lets this tool read, write, upload or delete something the person
running it did not ask for. That includes path traversal outside a directory the
user named, a dependency that exfiltrates data, a command injection through a
filename, or any network call at all, because these tools are local-only by
design and a network call is itself the bug.

## What is not in scope

These are command line tools and libraries that run locally with the privileges
of the person running them. Anything that requires already having control of
that machine or that account is not a vulnerability in this project.

## Supported versions

The most recent tagged release. This is a personal project, not a product with
a maintenance window, and I would rather say so than imply a support commitment
I will not keep.
