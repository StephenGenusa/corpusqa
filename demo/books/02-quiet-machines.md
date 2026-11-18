# Quiet Machines: A Practitioner's Concurrency

*by Desmond Okafor-Lyle, 2003*

## Chapter 1: Why Quiet Is the Goal

The best concurrent program is one that nobody talks about. It runs for months, the pager stays silent, and the only evidence of its threads is a graph that goes up when the load does. I call such systems *quiet machines*, and this book is about how to build them without heroics.

I have spent a good many years on teams that ship server software, and I have noticed a pattern. The engineers who write the loudest, cleverest concurrent code are seldom the ones whose systems stay up. The ones who do well pick the most boring tool that works, apply it consistently, and spend their cleverness elsewhere. I want to persuade you to be one of the second kind.

### What this book assumes

You know what a *thread* is. You have written a program that starts a few of them, and you have probably seen one misbehave. You do not need a theoretical background; you need a few rules of thumb, a set of tests to run, and enough understanding of the machine to know why the rules hold. I will give you all three, in that order of importance.

I will also be blunt about trade-offs. Concurrency is not free, and the cost is paid in bugs more than in cycles. Every technique in these pages can be misused, and most of them are advertised by people who have never been woken at three in the morning to explain a corrupted table.

### The shape of the book

The second chapter describes the problem in the form it takes on real servers. The third states my default, which is the plain mutex. The fourth examines the one place where I will allow something fancier, and the fifth is about measuring before getting clever. The sixth explains the hazards of many locks, and the seventh covers testing and running a quiet machine in production.

## Chapter 2: Where Races Come From

A *race condition* exists whenever the correctness of a program depends on a timing nobody has promised. On a server these arise from a very small number of patterns, and learning to spot them is most of the battle.

### The read-modify-write

The first and most common is the *read-modify-write* sequence. A thread reads a value, computes a new one from it, and stores the result. If another thread does the same in between, one of the two updates vanishes. A hit counter, a running total, a count of open connections, an inventory of tickets left for sale: all of these have this shape. Because the sequence reads as a single statement in most source code, the author seldom sees the danger.

### Check then act

The second pattern is *check then act*. A thread tests a condition and then does something that assumes the condition is still true. The ticket counter again: ask whether any seats remain, and, if so, sell one. Two threads can both see one remaining seat, and both sell it. The test and the sale each looked safe; the combination was not.

### Publication

The third pattern is subtler. A thread builds an object and then makes it visible to others by storing a pointer to it somewhere. If the store becomes visible to another thread before the contents do, the reader sees a half-built object. Processors and compilers are entitled to reorder operations that a single thread cannot distinguish, so this is not a theoretical worry. The remedy is to publish through a primitive that carries the proper guarantees, not through an ordinary store, and I will return to this when we discuss the mutex.

### A scenario: the ticket counter

Throughout the book I will use a small imaginary service, a counter that sells tickets to an evening concert. Customers arrive over the network, each handled by a worker thread. The service keeps a table of seats, a count of seats left, and a log of sales. Every piece of this is shared, and every piece can go wrong. By the end of the book the counter will run quietly, and I will point out what we did to get there.

## Chapter 3: The Default Is a Mutex

Here is my main advice, stated up front so that you cannot miss it. **Default to a mutex.** When you have shared data that more than one thread can change, protect it with a mutex, and think about something more elaborate only when you have a reason you can write down.

### What a mutex gives you

A *mutex*, short for *mutual exclusion*, is a lock that at most one thread can hold at a time. A thread that wants the protected data takes the mutex, works on the data, and releases the mutex. Any other thread arriving in the meantime waits. The code between taking and releasing is the *critical section*. A mutex also carries the memory guarantees that I mentioned in the last chapter: what one thread wrote before releasing the mutex is visible to the next thread that takes it. That bonus is easy to forget and very valuable.

Why default to it? Because it is easy to read and easy to reason about. A reviewer can look at a function, see the mutex taken at the top and released at the bottom, and conclude that the body is safe. The same reviewer, shown a hand-rolled scheme of flags and retries, will take an afternoon to reach a conclusion and may reach the wrong one.

### Protecting the ticket counter

In our concert service, the table of seats and the count of seats left are guarded by one mutex. A worker thread takes it, checks that a seat is free, marks it sold, decrements the count, and releases it. The check and the action sit inside the same critical section, so the check-then-act race disappears. **Guard every piece of shared mutable data with a mutex, and take it for the check and the action together.**

I like to attach the mutex to the data it protects, in the same structure, so that no one can touch the data without seeing the mutex beside it. A comment that says which lock guards which field is the cheapest documentation you will ever write.

### Keep it short

The second half of the advice is about duration. **Keep critical sections short.** A mutex is a queue with a single server, and the longer the server dawdles, the longer the queue. Do your preparatory computation before taking the mutex and your follow-up work after releasing it. If the critical section contains a loop whose length depends on the input, ask whether the loop can move outside.

### Recursion and its temptations

Many mutex implementations offer a *reentrant* variant, which lets a thread that already holds the mutex take it again without waiting for itself. It looks convenient, and I have found that it mostly excuses unclear design. If you cannot say which functions are called with the mutex held and which without, a reentrant mutex will hide the confusion. Prefer a design in which each function either expects the lock to be held or takes it, never both.

## Chapter 4: The Narrow Door for Atomics

Now to the exception. Modern processors offer *atomic* operations: single instructions that read and update a memory location indivisibly. An atomic increment adds one to a counter without any possibility of another thread seeing the intermediate state. A *compare-and-swap* replaces a location's value with a new one only if the location still holds an expected old value, and reports whether it succeeded. Software can build a retry loop on top: read the value, compute the new one, attempt the swap, and try again if someone got in first.

These operations are real and useful, and their allure is that they avoid taking a lock at all. That allure is a trap for the unwary.

### The rule

My rule is narrow. **Use atomics only for a single counter or a single flag.** A statistics counter that is bumped from many threads and read occasionally; a flag that says "shutdown has been requested"; a sequence number handed out in order. These are single words, updated independently of anything else, and an atomic operation does exactly what you want.

The moment you need two variables to change together, or you need to maintain a relation among several, the atomic stops being enough. Each atomic operation is indivisible by itself, but a pair of them is not. If you update a total and a count with two separate atomic operations, an observer can see a total that includes a sale and a count that does not. For anything involving more than one location, take a mutex.

### Why not use them everywhere?

The people who propose atomics for larger structures are usually right about speed and wrong about everything else. A structure built on compare-and-swap loops must handle the case where the swap fails, the case where the value changed and changed back (the *ABA* problem, in which the swap succeeds though the world is different), and the question of when memory can safely be freed. Each of these has bitten people who were clever. I would rather you spend your cleverness on your product.

### Memory ordering

Atomic operations come with a choice of ordering guarantees, and the weaker ones are faster and far harder to use correctly. If you do use atomics, use the strongest ordering your library offers until measurement tells you otherwise. The saving from the weaker ones is small, and the cost of a mistake is a bug you may never reproduce.

### The shutdown flag

For concreteness: in our ticket service a flag tells the workers to stop. Each worker reads it between requests. The flag is one word, written once by the main thread and read by many. An atomic flag is the right tool. The seat table, by contrast, stays under its mutex.

## Chapter 5: Measuring Before Getting Clever

The question I hear most is some form of "isn't a mutex slow?" It can be, and the answer is to find out rather than guess.

### Contention is the thing to watch

An uncontended mutex is cheap. On the machines of the present day, taking and releasing a free mutex costs about as much as a modest handful of ordinary instructions. The price rises when threads *contend*, meaning that several want the same mutex at the same time. Then some wait, perhaps by spinning and perhaps by sleeping and being woken, and sleeping costs a trip through the operating system.

So the question to ask is not whether a mutex is slow but whether this particular mutex is contended. **Measure contention before you try anything clever.** Instrument the mutex to record how often a thread had to wait and for how long, or use a profiler that reports time spent blocked. If the waiting is small, you are done. If it is large, then and only then consider remedies.

### The remedies, in order

The first remedy is to shorten the critical section, as discussed. This alone fixes most cases, because most long critical sections contain work that did not need protection.

The second is to split the data. A single mutex guarding an entire table can become several mutexes, each guarding a slice of the table, so that threads touching different slices do not meet. This is called *lock striping*, and it is effective. It does multiply the number of mutexes, and with them the chance of deadlock, which is the subject of the next chapter.

The third is to replace the mutex with a reader-writer lock when most accesses only read. This helps when reads vastly outnumber writes and the reads take a noticeable time. When reads are short, a reader-writer lock is often slower than a plain mutex, because its bookkeeping costs more than the exclusion it avoids. Measure this as well.

Only after these should you consider atomics or anything lock-free, and then only for the narrow cases of the last chapter.

### A parable of two weeks

One team I worked with spent two weeks replacing the mutex around a request counter with a lock-free arrangement. When they profiled the result, they found that the original mutex had been held for under a microsecond and contended almost never. The service was slow for an entirely different reason: a log message written inside a critical section. Two weeks of work and a more fragile program, for nothing. Measure first.

## Chapter 6: The Hazards of Many Locks

A system with one mutex is simple. A system with many is where trouble begins, and two hazards in particular deserve a chapter of their own.

### Deadlock

*Deadlock* is a state in which each of a group of threads holds a lock that another needs, and each waits for the others to let go. None ever does. In the smallest case, thread one holds lock A and wants B while thread two holds B and wants A. Nothing in the program reports the problem; it merely stops.

The cure I recommend is *lock ordering*. **Decide on a single global order for all your locks and always acquire them in that order.** If the seat table has a lock and the sales log has a lock, declare that the seat table comes first. Any thread needing both takes the seat table, then the log. A thread that holds the log and later needs the table has broken the rule and must be fixed. With one order obeyed everywhere, the circular wait that deadlock requires cannot form.

Write the order down in a document the whole team can see, and, if your tools allow it, add a debug check that complains when a lock is taken out of order. Violations are easiest to catch on the day they are written.

A second precaution: do not call code you do not control while holding a lock. A callback, a virtual function or a user-supplied handler might take locks of its own, in an order you have not seen. If you must call out, release your lock first.

### Never hold a lock across I/O

The second hazard is quieter and does more harm in practice. **Never hold a lock across I/O.** A disk read, a network request, a database query: each takes far longer than any computation, and its duration is not under your control. While the thread waits for the device, everything else that wants the lock waits behind it, and the processors sit idle. One slow disk can freeze a whole service. I have seen a server with sixteen processors stall completely because a single thread held the cache lock while waiting on a network call that never returned.

A lock held across I/O starves every other thread that needs it. The usual approach is to read what you need into a private copy, release the lock, perform the slow work, and, if necessary, take the lock again to store the result, checking that nothing relevant has changed meanwhile.

### Priority inversion

A related hazard bears a brief mention. If a low-priority thread holds a lock that a high-priority thread wants, and a medium-priority thread keeps the low one from running, the high-priority thread is stuck behind the medium one. Schedulers that support *priority inheritance* lift the holder's priority while it holds the lock, which resolves this. If you work with priorities, learn what your system does.

## Chapter 7: Testing and Keeping the Machine Quiet

Having chosen sound tools, you must still check your work, and then you must live with it.

### Testing concurrent code

A test that passes proves little about concurrent code, because the failing timing may simply not have occurred. A few practices improve the odds.

Run the tests with far more threads than there are processors. The scheduler then preempts at more awkward moments. Run them for a long time, repeatedly, and at different loads. Add small random delays inside the critical regions during testing, to widen the windows in which races can occur. A race you cannot make fail is a race you cannot be sure you have fixed, so spend effort making it fail first.

Where possible, test the invariants directly. In the ticket service, after a flood of simultaneous purchases, the number of seats sold must equal the number of seats originally available minus those remaining, and no seat may be sold twice. Checking this at the end of a stress test catches lost updates that no individual operation would reveal.

### Reading and review

Do not neglect the oldest tool. For each shared variable, list every place that touches it and the lock held at that place. Anywhere a lock is missing is a suspect. Code review for concurrency is mostly this exercise, performed by someone who did not write the code.

### Running in production

A quiet machine needs to be observable. Record, at a low rate, the length of time threads spend waiting for each important lock, and watch for it creeping upward as load grows. Record how many threads are busy. If a lock begins to dominate, you will see it in the figures before your customers see it in their queues.

Be wary of changes made in a hurry. The usual cause of a new deadlock is a new line of code that takes a lock out of the agreed order, added by someone who did not know there was an order. Keep the order written down, and tell every newcomer on their first day.

### What to take away

The advice of this book fits in a few sentences. Default to a mutex, and keep critical sections short. Reach for an atomic only when the shared state is a single counter or flag. Measure contention before you try anything cleverer. Acquire locks in one global order, and never hold a lock across I/O. Do these things consistently and your machines will be quiet, which is the highest praise I know for a program that runs on many threads at once.
