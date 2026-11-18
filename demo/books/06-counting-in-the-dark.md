# Counting in the Dark: Atomic Operations for Working Programmers

*by Saoirse Delacroix-Nwosu, 2008*

## Chapter 1: The Visitor Counter That Lied

Every programmer meets the same small humiliation sooner or later. You write a counter that records how many requests your service has handled, you run it under light load, and the number is perfect. Then you put it in front of real traffic, and an hour later the dashboard says nine hundred thousand when the access log says a little over a million. Nothing crashed. No error was raised. The counter simply lost track of some of the people it was counting.

This book is about that failure, and about a family of tools, called *atomic operations*, that exist largely to cure it. I have taught the subject to working programmers for most of two decades, and I have learned that the cure is easier to apply than to understand. So we will take the failure apart first.

Consider what "add one to the counter" really means to a processor. It is not one step. The processor reads the current value from memory into a register, adds one to the register, and writes the result back. Three steps, and the machine is free to interleave the steps of two threads that are running at once. Suppose the counter holds forty-one. Thread A reads forty-one. Thread B also reads forty-one. A writes forty-two. B writes forty-two. Two visitors arrived, and the counter moved by one. The second visit has vanished without a trace.

This pattern has a name, the *read-modify-write* hazard, and it is the most common form of a *race condition*: a situation in which the outcome depends on the unlucky timing of two activities that touch the same memory. Notice that the timing is what makes it dangerous. On a quiet machine the two threads rarely overlap, so the bug sleeps. Under load the overlap becomes frequent, and the bug wakes up, which is exactly when you can least afford it.

There are, broadly, two ways to protect such a counter. You may surround the three steps with a lock, so that only one thread at a time can be inside them. Or you may ask the hardware to perform the whole read-modify-write as one indivisible step. The second way is what we call an atomic operation, and the word comes from the Greek for "uncuttable": no other thread can observe the operation half-done.

I will say now, so that it is not lost in later chapters, which of the two I would reach for first in most situations. **Use a lock unless you have measured a reason not to.** Atomic operations are a sharp tool with a short list of legitimate jobs, and the rest of the book describes that list, and its edges, carefully.

## Chapter 2: What Atomic Means

An atomic operation is one that appears to every other thread to happen instantaneously, at a single point between its start and its finish. Before that point, the old value is visible. After it, the new value is visible. There is no moment when a thread can see a mixture.

The processor achieves this in various ways, depending on the design. Some instructions lock the memory bus or the cache line for the few cycles they need. Others use a protocol in which the processor tentatively claims exclusive ownership of the line, does its work, and gives it up again. You do not need the details. What you need is the contract: the operation completes as a unit, and the result is visible to other threads in a well-defined order.

That last phrase deserves a pause, because it hides a subtlety that has ruined many afternoons. Modern processors and compilers reorder memory accesses for speed. An atomic operation on one variable says nothing, by itself, about the order in which your *other* reads and writes become visible. Most atomic libraries therefore let you choose a *memory ordering*, a statement of how much surrounding activity the operation must keep in place. The strongest ordering is slower and the easiest to reason about. The weaker orderings are faster and treacherous. If you are a working programmer and not a specialist in hardware memory models, choose the strongest ordering your library offers, and do not weaken it until a profiler forces you to.

The simplest and most useful atomic is the *atomic increment*, often called fetch-and-add. It adds a number to a variable and hands you back the value the variable held just before. Because the add and the fetch are one step, two threads calling it at the same moment each get a distinct answer, and the final total is correct. Return to the visitor counter of the last chapter. Replace the three-step update with a single atomic increment, and the lost visits disappear.

For a single counter, an atomic increment is all the protection you need. No lock, no waiting, no risk of one thread stalling the others while it sleeps holding a latch. This is the clearest win in the entire subject, and it is worth remembering how narrow it is. The guarantee covers one variable and one operation. The moment your update touches two variables that must agree with each other, the guarantee evaporates, and you will need a different tool.

The same family includes atomic *exchange*, which stores a new value and returns the old one, and *test-and-set*, which sets a flag and reports whether it was already set. A flag that says "shutdown has been requested" is a fine candidate for this treatment. One thread sets it, the others read it, and nobody needs to coordinate beyond that.

## Chapter 3: The Compare-and-Swap Loop

Fetch-and-add handles addition. But suppose your update is something else: record the largest value seen so far, or advance a state machine from "starting" to "running" only if it is currently "starting". No hardware instruction exists for every such rule, so processors provide a general-purpose building block, *compare-and-swap*, usually abbreviated CAS.

CAS takes three things: a memory location, the value you *expect* to find there, and the new value you would like to store. It looks at the location. If the contents equal your expectation, it stores the new value and reports success. If they differ, it changes nothing and reports failure. The looking and the storing happen as one atomic step, so nobody can slip in between them.

The way to use CAS is a pattern so common that it has become idiom: the *retry loop*. You read the current value. You compute the value you want to write, based on what you read. You attempt a compare-and-swap, expecting the value you read. If it succeeds, you are done. If it fails, somebody else changed the location while you were computing, so you throw away your result, read the fresh value, and try again.

Take the "largest value seen" example. A thread reads the current maximum. If its own candidate is not larger, it stops, since nothing needs to change. Otherwise it attempts to swap the maximum from the value it read to its candidate. If another thread raised the maximum in the meantime, the swap fails, the thread rereads, and it asks the question afresh. Eventually either the thread wins or it discovers that someone else has stored something bigger. Either way the maximum is right.

Notice the optimism. A CAS loop does not stop others from working; it assumes that conflicts are rare and pays only when one happens. That makes it cheap under light contention. Under heavy contention many threads fail and retry repeatedly, and the cost climbs, though some thread always succeeds on each round. This guarantee of system-wide progress is called being *lock-free*. It is a weaker promise than it sounds: any individual thread may in principle lose the race again and again, and only the group as a whole is certain to move forward.

A CAS loop is also the foundation on which many locks themselves are built. A simple *spinlock* is a flag that a thread tries to flip from "free" to "held" with a CAS, looping until it succeeds. So the lock-versus-atomic dichotomy is not as sharp as I made it sound in the first chapter; the lock is often made of the atomic. But when you use a ready-made lock, someone else has already solved the difficult parts of that construction for you, and that is much of the point.

## Chapter 4: The ABA Problem

Now to the failure that every serious user of compare-and-swap must understand before writing a line of production code. It is called the *ABA problem*, and it works like this.

A thread reads a location and sees the value A. It goes away to do some work. While it is gone, a second thread changes the location from A to B, and then, later, changes it back from B to A. When the first thread returns and attempts its CAS, expecting A, it finds A, and the swap succeeds. The location looks exactly as it did before. But the world has not stood still, and the assumption that "nothing has changed since I looked" is false.

For a plain counter this hardly matters, because a number that has returned to its former value is simply that number. The trouble appears when the value is a *pointer* to something. Imagine a shared stack, built as a chain of nodes, with a single pointer to the top node. A thread wants to pop. It reads the top pointer, sees node A, and notes that A points down to node X. It plans to swing the top pointer from A to X. Before it does, another thread pops A, pops the next node too, and then pushes A back, perhaps after its memory has been recycled and reused. The top pointer holds A again, but A now points somewhere quite different. The first thread's CAS succeeds, installs the stale X as the new top, and the stack is now corrupt. Nodes have been lost or resurrected, and the program will fail somewhere far away, much later.

The standard defence is to stop the value from being able to repeat. Attach a *version tag*, a number that is incremented every time the pointer changes, and compare the pair together in one CAS. A has become A-version-seven, which no later A-version-nine can impersonate. Another family of defences refuses to reuse the memory of a node while any thread might still hold a pointer to it, deferring the reclamation until it is provably safe.

Both defences work. Both also add complexity, and each has its own failure modes. This is the first of several reasons for the caution that runs through this book. **If you cannot explain, in a sentence, why your compare-and-swap is immune to the ABA problem, you should not ship it.** Warn your colleagues about it in the code comments, and keep the structures small enough that a reviewer can hold the whole argument in her head.

## Chapter 5: Why Lock-Free Is Hard

Having shown you the tools, I owe you an honest account of why I rarely recommend building with them directly.

First, atomic operations compose badly. One atomic update to one variable is easy to reason about. Two atomic updates to two variables are not one atomic update to a pair. If an account balance and a transaction count must always agree, updating each atomically still lets another thread observe them mid-change, with the balance moved and the count not. A lock makes both changes visible together. An atomic cannot. When the invariant you need to protect involves more than one memory location, reach for a lock.

Second, lock-free code is hard to test. A bug in it may appear one time in a hundred million executions, and on exactly one model of processor. I have watched teams run a stress test for a week, declare victory, and then meet the bug on a customer's machine within a day. The reasoning has to be done on paper, and the paper is difficult.

Third, it is usually not faster in the way people expect. A contended atomic operation forces the processors to pass a cache line back and forth, and that traffic can cost as much as a lock's waiting. I have seen a lock-free counter, hammered by sixty-four threads, run slower than a modest locked version that let each thread count privately and merged the results at intervals. Measure before you decide.

So I give the rule once more, in the plainest terms I can manage. A lock is the right default. Reach for a bare atomic only for a single counter, a single flag, or a single reference that you replace wholesale, and only when a measurement shows that the lock is hurting you. Do not build a lock-free queue because it sounds impressive. If you need one, borrow a well-tested implementation from a library written by people who do this full time.

## Chapter 6: A Working Method

Let me close with the procedure I actually follow, written as prose instead of a checklist because the order of the thoughts matters.

I begin by naming the shared state. Which variables can more than one thread touch? Often the honest answer is smaller than the first guess, and every variable I can make private to one thread is one I no longer need to protect. I then ask, for each shared item, what invariant ties it to the others. A lone number with no relationship to anything else is a candidate for an atomic. Anything involved in a relationship goes behind a lock, and I keep the locked region as short as I can.

Next I write the simple, locked version, and I make it correct before I make it quick. Only then do I measure. I look for threads that spend their time waiting for a particular lock, and I ask whether that lock is protecting a single counter or flag. If it is, and if the waiting is real, I swap in an atomic increment or a small CAS loop, I add a comment that states the ABA reasoning, and I test it harder than I would test anything else in the program.

Most of the time the measurement shows no problem at all. The lock is fast enough, the code is plain, and the next person to read it understands it in an afternoon. That is a perfectly good outcome, and I have stopped apologising for it.

I will leave you with the dark of my title. Counting in the dark means doing arithmetic that other threads may disturb at any instant, without being able to see them. Atomic operations are a way of counting that survives the disturbance. They are a good way, within their narrow range, and a poor way outside it. Know where the edges are, and stay well inside them.

## Chapter 7: A Brief History of the Indivisible Step

It helps to know where these instructions came from, because the history explains their odd shapes. The earliest multiprocessor machines had no special support at all. Programmers who wished two processors to agree on anything relied on clever arrangements of ordinary reads and writes, and there were famous algorithms that achieved mutual exclusion this way, each a small marvel of reasoning, and each slow and fragile on real hardware that reordered memory accesses behind the programmer's back.

Hardware designers soon decided that the matter was too important to leave to cleverness. The first widely copied answer was test-and-set, a single instruction that read a flag and set it in one indivisible step. It was enough to build a lock, and for years locks were what it built. Later designs added fetch-and-add, which suited counters, and finally compare-and-swap, which proved so general that theorists were able to show it can implement any shared structure that waits for no one. That result is a beautiful one, and it is also the root of a good deal of misplaced enthusiasm, since being *able* to build something is not an argument for building it.

A second family of designs takes a different route, giving the processor a pair of instructions: one that reads a location and marks it, and one that stores to it only if no other processor has touched it in the meantime. Such a pair avoids the ABA problem in its pure form, because it detects any write at all, even one that restores the old value. It has its own quirks, since the store may fail for unrelated reasons, such as the cache line being displaced, and so it too lives inside a retry loop.

What all these designs share is the lesson of this book in miniature. The hardware gives you a small number of indivisible steps, each acting on one word of memory. Everything larger, every lock, every queue, every guarantee about two variables, is built by someone, from those steps, with care. When you take a library lock from the shelf, you are borrowing years of that care. When you write your own lock-free structure, you are agreeing to supply it yourself.
