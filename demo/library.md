# Demo library: an invented shelf of concurrency books

Fictional books for the README animation. Every title and author is invented;
each title was searched for (2026-11-18) and no real book carries it. The text
is original, written for this demo; technical points were checked against
Wikipedia's articles on race conditions, mutual exclusion, locks, deadlock,
linearizability, compare-and-swap, immutable objects, message passing, the
actor model and software transactional memory.

**Demo question:** What do the authors recommend for preventing race
conditions in concurrent code?

**Demo definition:** Include locks and mutexes, atomic operations, immutable
data, message passing, and any warnings about deadlock or lock contention.

## The answers the shelf is built to give

The program groups the hits; these are the distinct views the books hold, so
the grouped screen has something real to show.

| View | Books |
|---|---|
| Guard shared state with a lock (mutex); keep critical sections short | 1, 2, 5, 8 |
| Do not share: pass messages between threads or actors | 3, 8 |
| Make shared data immutable | 4, 8 |
| Atomic operations / compare-and-swap for simple counters and flags | 6, 2 |
| Warning: locks invite deadlock; always acquire in one global order | 1, 5, 7, 2 |
| Software transactional memory | 7 |

Books 9–11 are off-topic and are expected to be judged not relevant, though
each uses words like "lock", "race" or "thread" in other senses.

## The books

Each file in `books/` is Markdown: `#` title, `##` chapters, `*italic*` for
emphasis and terms (the reader renders it). `build_library.py` turns them into
`.docx` with page bookmarks, so hits show page numbers.

1. **The Lantern and the Latch: Notes on Shared Memory** — Ottilie Brannagh,
   1994. Measured, old-school. Locks are the "latch": every piece of shared
   state has exactly one latch, held for as short a time as possible.
   Deadlock: number your locks and always take them in ascending order.
2. **Quiet Machines: A Practitioner's Concurrency** — Desmond Okafor-Lyle,
   2003. Pragmatic. Default to a mutex; reach for atomics only for a single
   counter or flag; measure contention before getting clever; warns that a
   lock held across I/O starves everyone.
3. **Letters Between Processes** — Ingrid Vasquez-Holm, 2011. Argues against
   shared mutable state altogether: threads or actors own their data and
   exchange messages; a mailbox serializes access so no lock is needed.
4. **The Unchanging Value** — Teodor Marsh, 2016. Immutability: a value that
   never changes cannot race; share freely, replace instead of mutate;
   persistent data structures make copies cheap.
5. **Field Notes from the Scheduler** — Hollis Penwarden, 1989. War stories:
   a lost bank deposit (read-modify-write), a deadlocked print spooler. Fix
   was a lock around the balance, and a fixed lock order for the spooler.
6. **Counting in the Dark: Atomic Operations for Working Programmers** —
   Saoirse Delacroix-Nwosu, 2008. Atomic increment and compare-and-swap for
   counters and flags; the CAS retry loop; warns about the ABA problem and
   that lock-free code is hard: "use a lock unless you have measured".
7. **Transactions All the Way Down** — Bartholomew Ingle, 2014. Software
   transactional memory as the alternative to hand-placed locks; composes
   where locks do not; a long passage on how hand-placed locks deadlock.
8. **The Patient Server** — Ruth Abernathy-Coles, 2019. A service in
   production: mixes all three — immutable configuration snapshots, a work
   queue between stages, and one mutex for the cache, kept short.
9. **Cables, Closets and Subnets** — Garrick Pell, 2001. Small-office
   networking (cabling, DHCP, VLANs). Mentions locking the wiring closet and
   a "race" of DHCP offers. Not relevant.
10. **The Keyboard Collector's Almanac** — Wynn Halloran, 2020. Mechanical
    keyboards: switches, keycaps, the Caps Lock key. Not relevant.
11. **Ledgers in the Machine** — Philippa Strand, 2005. Spreadsheet practice
    for small firms: locking cells, shared workbooks and the conflicts two
    editors cause (a non-programming "race"). Not relevant.
