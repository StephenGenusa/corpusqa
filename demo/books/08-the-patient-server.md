# The Patient Server

*by Ruth Abernathy-Coles, 2019*

## Chapter 1: Why We Built a Slow Service on Purpose

The service I am going to describe is called Harbour, and its job is unglamorous. Merchants send it batches of product listings, thousands at a time. Harbour checks each listing against a set of rules, enriches it with category data and tax information, stores the result and tells the merchant what it did. In its first year it processed about forty million listings. In its third year it processed that many in a week.

I was the engineer on call for most of that growth, and this book is what I learned about keeping such a service correct while it was being made faster. I want to say at the outset that Harbour was never a showcase of clever concurrency. We chose, over and over, the dullest tool that worked. The title comes from a remark by a colleague who read our design notes and said that the server seemed to be a very patient thing, doing one small step at a time and never in a hurry. I took it as the highest compliment available.

The service ran as a single process on several identical machines, each with many threads. Requests arrived, were decomposed into stages, and passed through a pipeline. There were three areas where threads genuinely met: the configuration the stages consulted, the hand-off of work from one stage to the next, and a cache of category lookups that every stage wanted. Over the years we settled on a different treatment for each, and the reasons matter more than the particulars. One tool for each kind of sharing; no tool applied out of habit.

I will give the shape of the argument now and flesh it out. For data that is read everywhere and changes rarely, we used immutable snapshots. For work travelling between stages, we used queues. For a small structure that really had to be shared and mutated, we used a single mutex, held for the shortest time we could manage. Three problems, three answers. When people ask me which concurrency technique I recommend, I say that the question is a bit like asking which tool in the shed I recommend.

A word on how the early days went, because they were instructive. The first version of Harbour had no design for concurrency whatever. It had a thread pool and a lot of objects. The code worked on a developer's laptop with four listings in a batch. In production, with four thousand, it produced bad data about one batch in several hundred, and the bad data was always plausible, which is the worst kind. The remaining chapters describe how we climbed out of that.

## Chapter 2: The Anatomy of a Bad Batch

Before the cures, the disease. Our first serious incident arrived on a Monday, when a merchant noticed that a handful of their listings carried the tax rate of a different country. Not wildly wrong rates, but rates belonging to a neighbouring jurisdiction.

The investigation took four days. We eventually found that a stage in the pipeline held a shared object describing the merchant's *context*: country, currency, tax regime. Each stage read it. A separate part of the system, the one that applied a merchant's updated settings, rewrote the fields of this object as the new settings arrived. If an update landed while a batch was in flight, then the stage that read the country early in a listing's processing and the stage that read the tax regime late could see different generations of the context. The listing was processed under a country from before the update and a tax regime from after it.

The reader may recognize this as a form of *race condition*, in which the outcome depends on a timing that nobody chose. The specific variety is a *torn read*, where related values are read at different moments and combined as if they were a single moment. No lock had been forgotten, strictly speaking, because no lock had ever been considered. The designers, of whom I was one, had assumed that settings changed so seldom that the chance of a collision was negligible. At a thousand batches an hour, negligible is a few times a week.

I would like to record the emotional course of such an incident, since textbooks leave it out. First comes disbelief, because the code looks right and the logs look right. Then comes the long, specific dread of realizing that the logs cannot be right, because the bug depends on a thing the logs do not record, the exact order of two events a few microseconds apart. Finally comes a sort of respect for the problem. A fault that lives in timing is a fault that no amount of reading the code in a quiet room will expose, and one should not feel too foolish for missing it.

We did several things that week that did not help and I list them as a warning. We added logging, which changed the timing and made the fault rarer. We increased the number of threads in an attempt to reproduce it, which made it more common but no easier to pin down. And we considered, for one dark afternoon, adding a global lock around all of batch processing, which would have fixed the data and roughly halved the throughput. We did not do it, and the next chapter explains the alternative we chose instead.

## Chapter 3: Configuration as a Snapshot

The merchant context was the first thing we repaired and the pattern we used there became the pattern for the whole service. We stopped letting anyone change the context object. Instead, we made it an *immutable snapshot*: a value built once, in its entirety, and never altered after construction.

Updating a merchant's settings now meant building a fresh snapshot from the old one and the new settings, validating it, and then replacing the system's pointer to the current snapshot with a pointer to the new one. The replacement is a single indivisible step, which our platform provides as a basic operation. A batch picks up the snapshot that is current when the batch begins and carries it through every stage. **Give each batch one immutable snapshot of the configuration at the start, and read from that snapshot until the batch ends.**

Consider what this removed. A batch cannot see a half-applied update, because updates are never applied in halves; a new snapshot either exists or it does not. A batch cannot see two generations, because it holds exactly one. Readers need no lock, because a value that cannot change is safe to read from any number of threads at any time. And a debugging engineer gets a gift: each batch's log can record the version number of its snapshot, and from that alone we can reconstruct precisely the settings that governed it.

There is a subtle cost, and a subtle trap. The cost is that a merchant who changes a setting will see the change take effect for the *next* batch and not the current one. We discussed this at length with the product team and found, to our relief, that nobody wanted it any other way. A batch processed under two sets of rules is a defect. A batch processed entirely under the old rules, a few seconds before the new ones took over, is just an event with a timestamp.

The trap is the one that catches nearly everyone: an object that looks immutable but holds a reference to something that is not. Our first snapshot type contained a list of allowed categories, and the list was an ordinary resizable one, which one of the enrichment stages cheerfully sorted in place. We found it in review. **Make a snapshot immutable all the way down: its lists, its maps and everything they contain must refuse modification too.** We now construct these parts as unmodifiable views over private copies, and a test that tries to change them fails the build if it succeeds.

I commend the pattern to anyone whose service has configuration, feature flags, price tables, routing rules or anything else that is read constantly and changed occasionally. It costs almost nothing at read time and it removes a whole class of timing questions from your life.

## Chapter 4: Queues Between Stages

The second place that threads met was in the handing of work from stage to stage. A listing was parsed, then validated, then enriched, then stored. In the first design, each of these steps was a method called in turn by the same thread, with shared helper objects holding intermediate results. In the redesign each stage became its own small group of threads, and between each pair of neighbouring stages we placed a *queue*.

A queue, in this sense, is a first-in, first-out channel safe for use by many producers and many consumers. The parser puts a parsed listing on the queue that leads to the validators. A validator takes one off, does its work, and puts the result on the queue that leads to the enrichers, and so on. The point that matters is that a listing is only ever in one stage's hands at a time. The parser, having handed it on, never touches it again. The validator, having received it, holds the only reference. **Pass each unit of work between stages through a queue, and let only the stage holding it modify it.**

It took us a while to appreciate how much this changed our thinking. Previously, any helper could be reached from any stage, and the question of who might be changing what required reading the whole pipeline. After the redesign, the question was answered by the drawing on the whiteboard. The data lived with whichever stage had it, and it moved along arrows.

We made a handful of decisions about queues that I would repeat. First, *bound them*. An unbounded queue between a fast stage and a slow one will absorb the difference until memory runs out. A bounded queue makes the fast stage wait when the slow stage falls behind, which is called *backpressure*, and it turns an overload into a slowdown instead of a crash. We sized ours by asking how many seconds of work we were willing to lose if a machine died, and then converting that into a number of items.

Second, *make the items small and self-contained*. A queue entry should carry everything that the next stage needs, including its configuration snapshot, so that the next stage never has to reach back for data. Third, *hand over, don't share*. If a stage wants to keep a copy for its own records, it makes one; it does not keep a reference to something it has already passed along.

The performance results were a pleasant surprise. We had expected the extra hand-offs to cost throughput, and they did cost some, in the order of a few percent. But the stages could now each be tuned independently, and the slowest stage, enrichment, could be given more threads without touching the others. The overall rate rose by more than half.

## Chapter 5: One Mutex, and a Short One

I have saved for last the part of Harbour that I like the least, because it is the part that is least tidy. Every stage looked up category data, such as the tax class and the display name for a category, and these lookups hit a database that was slow. We kept a cache of recent answers in memory, a map from category identifier to its details, and all stages wanted to read it and add to it.

We examined several elegant options. Immutable snapshots did not fit well, because the cache changed on nearly every request, and rebuilding the map each time would have cost more than the database trip. A single owning thread with a queue of requests was possible but turned a nanosecond lookup into a message exchange. In the end we did the plain thing: a *mutex*, a lock that only one thread can hold at a time, guarding the map. **Put one mutex around the cache and hold it only for the lookup or the insertion, never for anything else.**

The discipline is entirely in the second half of that sentence. The first version of our cache code took the lock, looked for the entry, and, on a miss, fetched it from the database while still holding the lock. During that fetch every other thread that wanted anything from the cache waited, including threads that wanted entries which were already present. The fix was to take the lock, check for the entry, release the lock, do the slow fetch with no lock held, and then take the lock again briefly to insert the result. Occasionally two threads fetch the same entry, at a small cost in duplicate work; we accepted this, because a duplicated lookup is cheap and a convoy of waiting threads is not.

We also followed a few smaller rules. **Never call out to unknown code while holding the lock.** Anything that might block, log to a slow destination, or take another lock has no place inside the critical section. We kept a single lock for the single cache, so the question of ordering several locks never arose, and **if you ever need two, always take them in the same order**, a rule that we documented even though we never needed it.

I should mention contention once and honestly. *Contention* is what happens when many threads want the same lock at once, so that they spend their time queuing instead of working. At our peak, with sixty-four threads, the cache lock was the single busiest point in the service, and profiling showed threads waiting on it for several percent of their time. This is the price of shared mutable state, however short the hold. We judged it acceptable, since it was bounded and well understood, and we kept a metric on it, so that we would hear about it if it grew. When it did, our response was to split the cache into several independent pieces, each with its own lock, by category, instead of looking for something cleverer.

## Chapter 6: Deploying, Draining and Staying Calm

The final matter is how to put such a service into production without breaking it, which turns out to interact with concurrency in ways we did not anticipate.

When we deployed a new version of Harbour, the old version had batches in flight. We learned to *drain*: a machine due for replacement stopped accepting new batches, let the existing ones finish, and only then shut down. Draining relied on the queues. Each stage, told to stop, finished the items already in its input queue and refused no more, and the pipeline emptied from front to back. A system whose work lives in explicit, visible queues can be stopped gracefully because you can see how much is left. A system whose work is scattered in the stacks of threads cannot.

Testing was the other great lesson. We found that the most valuable concurrency test was neither clever nor exotic. It was a *soak test*: run the service at several times its normal load for hours, with a checker that verified every output against an independent computation. Races rarely appear in a short test and often appear in a long one. We also learned to run the unit tests with deliberately perturbed timing, adding small random delays at the points where threads hand work over, which turned timing faults from monthly into hourly events.

Observability mattered too. Each batch carried its snapshot version, and each queue reported its depth, and each lock reported how long threads waited on it. When something went wrong we could usually tell from the graphs which of the three kinds of sharing was involved, and the three kinds had three different fixes.

If I had to condense five years into a paragraph, it would run like this. Decide for each piece of shared data which kind it is. If it is read often and changed seldom, make it a snapshot. If it is work in motion, put it on a queue. If it is truly a shared structure, put a short mutex around it and keep the critical section small. Resist the urge to find one universal answer, because there is not one. And when a service is humming along and nobody is paging you, resist the urge to improve it without a measurement. The patient server wins by being boring, and boring is something you have to keep choosing.
