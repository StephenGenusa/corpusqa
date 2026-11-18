# Transactions All the Way Down

*by Bartholomew Ingle, 2014*

## Chapter 1: A Transfer Between Two Accounts

Let me begin with the smallest problem that still teaches something. A small bank keeps its accounts in memory, and a program must move money from one account to another. The operation has two halves: take an amount out of the first account, put it into the second. Every programmer who has written it has felt, correctly, that the two halves belong together. If the program is interrupted between them, money has either vanished or been created, and an auditor will want to know why.

Now run that program on several threads at once, serving many customers. Another thread, adding up all balances for a report, may look at the accounts after the withdrawal and before the deposit, and conclude that the bank is short. A second transfer may start from the same account and read a balance that is about to be out of date. These are *race conditions*, and they all grow from one root: a group of changes that ought to appear as a single event is instead visible in pieces.

The traditional remedy is to protect each account with a *lock*, a token that only one thread can hold at a time. A thread that wants to change an account takes its lock, makes the change, and releases the lock. This works beautifully for one account. The trouble starts when the operation touches two. To transfer, you need both locks, and the moment you hold one lock while asking for another, you have opened a door to a problem so old and so dependable that it has its own name. We will spend a long chapter on it.

This book argues for a different way of thinking. Instead of asking the programmer to decide which locks to take, in what order, for how long, we let the programmer say only *which steps must happen together*, and let the runtime work out the rest. The mechanism is called *software transactional memory*, or STM, and the idea is borrowed from the databases that have run the world's accounts for decades. A transaction is a bundle of reads and writes that takes effect entirely or not at all, and that no other thread can see half-finished.

I will not pretend that STM is free, or that it suits every program. Parts of this book describe its costs plainly. But I hold, and will try to persuade you, that for code in which several pieces of shared state change together, transactions are a better starting point than hand-placed locks. The title of the book is a small joke about how far that conviction goes.

## Chapter 2: How a Transaction Works

Strip away the vocabulary and an STM transaction is an *optimistic* gamble. The thread assumes no one will interfere, does its work, and checks at the end whether the assumption held.

Here is the sequence in plain terms. When a transaction begins, the runtime sets up a private notebook for it, often called a *log*. As the transaction reads a shared variable, the runtime notes in the log which variable was read and what value was seen. When the transaction writes a variable, the new value goes into the log, not into shared memory. Nothing has yet been made visible to the other threads. If the transaction reads a variable that it has itself already written, the runtime serves the value from the log, so that the transaction sees its own pending changes.

At the end comes the *commit*. The runtime checks every variable the transaction read, to confirm that each still holds the value that was recorded. If all are unchanged, then the world the transaction saw is still the world that exists, and the runtime publishes all the writes in one indivisible step. If anything changed, the transaction has been working from stale information. It is *aborted*: the log is discarded, none of its writes ever escape, and the whole body runs again from the start with fresh reads.

Several consequences follow, and a careful programmer keeps them in mind. First, because a transaction may run more than once, its body must not do anything that cannot be undone. Sending an email, printing a line, or charging a card inside a transaction is a mistake, since the action could happen twice. Keep the body to reads and writes of transactional variables, and defer the outside world until after the commit.

Second, a transaction that takes a very long time is more likely to be invalidated by someone else, so it tends to abort and restart more often. Long transactions that touch popular variables can fall into a state of near-starvation. The remedy is the same as for any shared resource: keep the work short and the footprint small.

Third, the guarantee the programmer receives is the same one a database gives: the transactions behave as if they ran one at a time, in some order, even though they actually overlap. That is a very strong guarantee, and it is the reason a transfer written as a transaction cannot be seen half-done by the report thread.

## Chapter 3: The Locks We Place by Hand

Before praising transactions further, I owe the old method an honest hearing. The lock is a sound idea, and nothing in this book is meant to deny it. A lock around a small piece of data, held briefly, is often exactly right. What I object to is the practice of placing many locks by hand across a large program, and asking every programmer to remember the rules by which they interact.

Consider the transfer once more, written with a lock on each account. A careful programmer writes it so: take the lock of the source account, take the lock of the destination account, move the money, release both. It passes every test. It runs for months.

Then two customers each send money to the other at almost the same instant. Thread one holds the lock on account A and asks for the lock on account B. Thread two holds the lock on B and asks for the lock on A. Neither can proceed, because each is waiting for something the other holds, and neither will let go until it finishes. The program does not crash, and it does not produce a wrong answer. It simply stops, for those two threads, forever. This is *deadlock*, and the unpleasant truth about it is that it needs bad luck to occur. A test suite will almost never supply the exact timing, so the defect survives into production.

Theorists have shown that deadlock requires four conditions at once. Access must be exclusive, so that only one thread can hold a resource. Threads must hold some resources while waiting for others. A held resource cannot be forcibly taken away. And there must be a circular chain of waiting, each thread in the chain wanting what the next one holds. Remove any one of the four and deadlock becomes impossible. In practice the condition that programmers can most easily remove is the last: the circle.

The discipline that breaks the circle is called *lock ordering*. Assign every lock a position in a single global order; give each account a unique number, for example. Then require that every thread, whenever it needs several locks, takes them in ascending order of that number. A thread that wants accounts 7 and 3 takes 3 first, then 7. With this rule, the scenario above cannot occur: both threads reach for the lower-numbered account first, and one of them simply waits its turn while the other completes. A circular chain would require some thread to hold a high lock while asking for a lower one, and the rule forbids that.

Always acquire locks in one agreed global order. That sentence is the whole of the discipline, and it is simple to say and demanding to keep. The order must be known to every piece of code that takes locks. It must be respected by code written next year by someone who never read this chapter. And it must survive refactoring: the day a function that holds one lock calls a helper that, three layers down, takes another, the ordering is silently at risk.

Other defences exist. A thread may try for a lock without waiting and give up everything it holds if the attempt fails, then begin again after a pause. A thread may wait only for a limited time. These approaches trade deadlock for a different hazard, called *livelock*, in which threads keep backing off and retrying in step with each other, busy and getting nowhere. They are tools of last resort.

## Chapter 4: Why Locks Do Not Compose

Lock ordering solves deadlock when you can see all the locks. The deeper trouble is that you often cannot, and this brings me to the central claim of the book.

A good program is built from pieces. We want to take a well-tested withdraw operation and a well-tested deposit operation and combine them into a transfer, without opening up either piece and reading its insides. This property, that correct parts can be assembled into a correct whole, is called *composability*, and it is what makes large software possible at all.

Locks fail this test. Suppose the withdraw function takes the account's lock internally, and so does the deposit. Calling one after the other gives a transfer that is individually safe at each step, and wrong as a whole: between the two calls, the money exists nowhere, and the report thread may see it. To fix that you must reach inside both functions, remove their private locks, expose the locks to the caller, and arrange for the transfer to take both in the proper order. You have broken the encapsulation that made the pieces reusable, and every caller must now understand the locking policy of every callee.

Transactions repair this directly. A transactional withdraw is a block of reads and writes on transactional variables. A transactional deposit is another. A transfer is simply a transaction that runs one and then the other, and the runtime treats the entire combination as a single transaction. The inner pieces need no modification and know nothing of the outer one. **Transactions compose where locks do not.** That is the sentence I want you to carry out of this chapter.

The same property gives STM two further abilities that locks cannot easily match. The first is *blocking within a transaction*. A transaction that cannot yet proceed, because the account lacks sufficient funds, can declare that it must wait. The runtime rolls the transaction back and suspends the thread until one of the variables it read has changed, then runs it again. The second is *choice*. A programmer can write that a transaction should try one alternative, and if that alternative has to wait, try another instead. Both of these, bolted onto lock-based code, tend to produce the most delicate and bug-ridden passages in any program.

Because the runtime, not the programmer, decides how conflicts are detected, no lock order needs to be remembered, and the four deadlock conditions never line up. A system that never holds one resource while waiting for another cannot deadlock on those resources. I recommend software transactional memory over hand-placed locks for any code in which several shared values must change together.

## Chapter 5: Costs, Limits and Honest Doubts

A book that sold only advantages would be a pamphlet. Here are the costs.

The bookkeeping is real. Every read and write inside a transaction goes through the log, and the commit must validate it. For a tiny update to a single value, a lock, or a lone atomic operation, is cheaper, and I would not use a transaction to increment one counter. The cost of STM is best justified where it removes complexity, and complexity is concentrated in operations that touch several pieces of state.

Contention hurts. When many threads write the same variable, transactions keep invalidating one another, and the runtime spends its time on aborted work. A design that funnels everything through one hot variable will perform badly under any scheme, but transactions can make the badness more dramatic. Spread the state across many variables so that unrelated transactions touch unrelated data.

Side effects must stay outside, as described earlier. Languages and libraries differ in how they enforce this, and some will refuse to let an irreversible action run inside a transaction. Others merely trust you. Learn which kind you have.

Last, STM is not yet available everywhere. Where it is missing, the sensible fallback is a small number of locks, carefully documented, with a published ordering, kept around small and clearly named pieces of data. I would rather see that than an improvised lock-free structure.

## Chapter 6: Practical Advice for the Reader

I will finish with a handful of habits, in the order I would adopt them.

Begin by drawing a line around the data that changes together. If the changes are independent, keep them independent and let each have whatever simple protection suits it. If they change together, make that group the unit of a transaction, and do not let it grow large.

Write each operation as a small transactional function with no side effects. Let the callers combine them freely. When a combined operation is needed, resist the urge to write new low-level code; compose the existing functions inside one outer transaction.

Where you must still use locks, because your platform offers nothing better, number them. Write the numbering down in a comment at the point of declaration, and in a document that new team members read. Take locks in ascending order, always. Release them as soon as the protected work is done.

And when something goes wrong, remember what a deadlock looks like from outside: a program that is quiet, not noisy. No error appears in the log. The threads are simply waiting. Most runtimes can print the state of every thread and the lock each one is waiting for, and a circular chain in that report is the signature. Learn to read it before you need to.

The reason for the title is now, I hope, clear. At the bottom of every safe program there is some form of transaction: a group of changes that become visible together or not at all. The question is whether you build it yourself from locks, one careful piece at a time, or ask the runtime to give you the whole. I have made my choice, and I have tried to show you why.

## Chapter 7: Where Transactions Came From

The idea is older than the computers on which we now use it. Commercial databases adopted the transaction decades ago, because their customers could not tolerate half-finished updates to records of money, stock and bookings. The designers summarised what they wanted in four words: *atomicity*, so that a transaction takes effect entirely or not at all; *consistency*, so that it carries the data from one valid state to another; *isolation*, so that concurrent transactions do not see one another's intermediate steps; and *durability*, so that a committed result survives a crash. Memory held only in a running program does not need the last property, which is why the transactional memory of this book is sometimes described as a database with the durability taken out.

The step from databases to programs came when researchers noticed that processors were gaining cores faster than programmers were gaining the patience to place locks. They asked whether the optimistic approach of databases could be applied to ordinary variables. The early answers were hardware proposals, in which the cache itself tracked what a transaction had read and written, and aborted when another processor interfered. Software versions followed, because they could be shipped without waiting for new chips, and these are the systems discussed in this book. Some modern processors do offer hardware support, and good libraries use it when it is present, falling back on the software method when a transaction grows too large for the hardware to track.

I mention the lineage because it answers a common objection. Sceptics say that transactional memory is an academic fancy. It is the application, to a new place, of an idea that has already been tested by every bank and airline on earth. The novelty lies only in the setting, and in the discovery that the same bargain, a little overhead in return for a great deal of clarity, pays there as well. Where the setting is a program whose state changes in groups, I advise using transactions to protect those groups, in preference to inventing a locking scheme of your own.
