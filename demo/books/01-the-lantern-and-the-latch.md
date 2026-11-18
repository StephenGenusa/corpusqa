# The Lantern and the Latch: Notes on Shared Memory

*by Ottilie Brannagh, 1994*

## Chapter 1: A Lantern in the Machine Room

When I first learned to reason about programs that do more than one thing at a time, my teacher gave me a lantern and sent me into a dark room. The room held a long table, and on the table lay a ledger. Two clerks were meant to work at that table, each carrying a pencil, and my task was to describe everything that might go wrong. I have kept the image ever since, because it contains the whole subject. The ledger is *shared state*. The clerks are *threads of execution*. The lantern is the only light you will ever have on the problem, and it shows a small circle at a time.

A single clerk working alone is easy to follow. He reads a figure, thinks, and writes a new figure, and the ledger is always in some definite condition between his steps. Add a second clerk and the circle of light stops being enough. Each clerk's steps are still in order, but the steps of the two are interleaved by a scheduler that neither clerk controls, and the interleaving can differ from one run to the next. A program that worked a thousand times may fail on the thousand-and-first, and when you return with your lantern the room looks perfectly innocent.

This book is a set of notes on how to live in that room. It does not promise to make concurrent programming easy; I have never found it so. It promises a small number of habits which, if kept without exception, make it tractable. I will name the habits plainly as we go, and I will try not to confuse them with the interesting but optional matters that fill the margins.

### What a race is

A *race condition* is a flaw in which the result of a computation depends on the relative timing of events that nobody has constrained. The classic form is the lost update. Two threads each read a counter, each add one to what they read, and each write the sum back. If the reads both happen before either write, the counter rises by one where it should have risen by two. Neither thread did anything foolish in isolation. The fault lives in the gap between the read and the write, and that gap is invisible in the source text, where the whole operation looks like a single line.

I find it useful to distinguish the *data race*, which is two unsynchronised accesses to the same location of which at least one is a write, from the broader family of timing bugs. The two overlap but are not the same. A program can be free of data races in the strict sense and still behave wrongly, because it checks a condition under one grant of exclusion and acts on it under another. Hold that thought; it returns in the fourth chapter.

## Chapter 2: How We Came to Share

It is worth recalling why programmers share memory in the first place, since the practice is the root of all the trouble. The earliest machines ran one job at a time. An operator loaded a deck of cards, the machine ran it, and the next deck followed. The expensive processor sat idle whenever the job waited for a tape or a printer, and nobody who paid for such a machine could bear the waste.

The remedy was *multiprogramming*: keep several jobs in memory and, when one waits for a slow device, switch to another. Switching was at first a rare event, performed only when a job asked for it. Later the hardware gained a timer that interrupted the running job and handed the processor to the scheduler, which chose who ran next. This is *preemption*, and it is the moment at which the clerk's pencil is taken from his hand in the middle of a sentence.

With preemption came the observation that jobs which cooperate are far more useful than jobs which merely coexist. A print spooler takes requests from many programs and feeds them to one device. A reservation system lets many agents sell seats from one pool. These cooperating activities need to talk, and the cheapest way to talk is to leave a note in memory that both can see. Sharing was not a mistake. It was the economical choice, and it remains so, which is why we cannot simply wish it away.

### From processes to threads

For a time, each activity had its own private address space, and sharing required an explicit request to the operating system. Then designers noticed that much of the cost of switching came from changing address spaces, and they invented the *thread*: a flow of control that lives inside a process and shares its memory with sibling threads. Threads are cheap to create and cheap to switch, and they make sharing the default rather than the exception.

I do not think this was wrong, but I think it moved the burden. Under the older arrangement the programmer had to ask for sharing, and the asking was a reminder. Under threads, everything reachable is shared unless you take pains otherwise, and the reminder is gone. The programmer must supply it from habit.

## Chapter 3: The Latch

I call the basic instrument of mutual exclusion a *latch*, after the simple bar on a gate. The gate is either latched or free. A visitor who finds it latched must wait; a visitor who finds it free lifts the bar, passes, and lowers it behind him. Other books call this a *mutex*, short for *mutual exclusion*, or simply a lock. The words are interchangeable for our purposes, and I use mine because it reminds me of something small and plain and made to be held only briefly.

The region of code that runs while the latch is held is the *critical section*. At most one thread may be inside it at a time, and that guarantee is the whole of what the latch provides. It does not make the code faster, or cleverer, or correct in any wider sense. It makes the stretch between lifting and lowering indivisible with respect to every other thread that respects the same latch.

The last clause matters. A latch protects only against threads that ask for it. A thread that touches the shared data without lifting the bar defeats the arrangement entirely, and nothing in the machine will complain. Exclusion is a convention, and conventions are kept by people.

### One latch for each piece of state

My first and firmest recommendation is about assignment. **Every piece of shared mutable state has exactly one latch that guards it.** Not two, and not none. If you can write a sentence naming the data and the latch that protects it, you have a design; if you cannot, you have a hope.

Two latches over one datum is the quieter error. The author reasons that his two routines each take a latch, so all is well, but the two latches are different ones, and so the routines exclude nothing from each other. Zero latches is the louder error and tends to be found sooner. Whatever the case, write the pairing down, in a comment beside the declaration of the data, in plain words: this field is guarded by that latch. The next reader, who is often yourself in six months, will be grateful.

### Guard the invariant, not the variable

A related point concerns what the latch is really protecting. It is easy to say that it protects a variable, but variables are rarely the true subject. The true subject is an *invariant*, a relation among several pieces of data that must hold whenever anyone looks. In a bank, the invariant might be that the sum of the account balances equals the total held by the bank. Moving money from one account to another breaks the relation for a moment, between the subtraction and the addition, and the latch exists to hide that moment from observers.

It follows that every piece of data taking part in an invariant must be covered by the same latch, or by latches taken together. Guarding each balance separately does not guard the sum. This is why I tell my students to start from the invariants and let the latches follow, rather than start from the variables and hope the invariants take care of themselves.

## Chapter 4: The Discipline of the Latch

Now we come to the practice, which is where good intentions are tested. The latch is cheap in itself. What it costs is the waiting of other threads, and so the principal virtue in using it is brevity.

### Keep the hold short

**Hold the latch for as short a time as you can.** Take it, do the one thing that needs protecting, and release it. Every instant you hold the bar is an instant in which every other thread that wants the same data must stand at the gate. If ten threads want it, your carelessness is multiplied by ten.

A practical test is to read the critical section aloud. If it contains a sentence that begins with "then it calls" and ends with something you did not write, it is probably too long. Prepare your values before you take the latch, and finish your leisurely work after you release it. Inside, do only what must be indivisible.

### Never across a disk read

The most damaging violation is the slow operation. **Never hold a latch across a disk read, a network call, or any other operation whose duration you do not control.** A memory access takes well under a microsecond; a disk read takes thousands of times that, and a network call to a distant machine more again. While your thread waits on the device, still gripping the bar, the other threads wait on you, and the machine, which has plenty of idle processor, accomplishes nothing.

The usual escape is to copy what you need while holding the latch, release it, and then perform the slow work on your private copy. If the result must be put back, take the latch again, check that nothing relevant has changed in the meantime, and store it. The extra care is repaid many times over.

### Check and act together

Here is the promised return to the broader family of timing bugs. Suppose a routine asks whether a seat is free and, finding that it is, reserves the seat. If the question and the reservation are made under two separate grants of the latch, another thread can slip between them and reserve the same seat. Each access was protected, and the program is nonetheless wrong. **Make the check and the action under one continuous hold of the latch.** The pair is the critical section, not either half of it.

### The story of the two tellers

Let me make this concrete with a case I have used for years. A bank has one counter and two tellers, and both are serving customers of the same account. The balance stands at one hundred. One customer deposits fifty and the other deposits thirty. Each teller reads the balance, adds the amount in his head, and writes the total on the slip. If the tellers read at the same moment, one writes one hundred fifty and the other writes one hundred thirty, and whichever writes last wins. The bank has lost money, or a customer has, and no teller made an arithmetic mistake.

The cure is the latch around the balance. A teller lifts the bar, reads, adds, writes and lowers it. The second teller waits for a few seconds, which is a trivial cost, and then reads the corrected figure. Notice that nothing about the arithmetic changed. The only change is that the read-add-write became one indivisible step.

## Chapter 5: When the Latches Quarrel

Having praised the latch, I must now describe what it does when many of them are in play. The hazard is *deadlock*, a state in which a group of threads each hold something another needs and each wait for what another holds, so that none can ever proceed.

The smallest example uses two latches and two threads. The first thread takes latch A and then asks for latch B. The second takes latch B and then asks for latch A. If each completes its first step before either begins its second, each now holds the thing the other wants. They will wait forever, politely, and the program will simply stop, with no error message and no useful trace.

Four conditions are needed for this to happen: the resources are held exclusively, a thread holds one while waiting for another, nothing can take a resource away from its holder, and the waiting forms a cycle. Remove any one of the four and the deadlock is impossible. Of the four, the cycle is the easiest for a programmer to remove.

### Number your latches

My remedy has been the same for many years, and it is dull enough to be trusted. **Number your latches, and always take them in ascending order.** Give each latch a rank, written down where all can see it. A thread that needs two latches takes the lower-numbered first. A thread that holds the latch numbered seven may not ask for the latch numbered four; if it needs four, it must release seven, take four, and take seven again.

The argument for the rule is short. A cycle of waiting would require some thread to hold a high-numbered latch while waiting for a lower one, and the rule forbids exactly that. No such cycle can arise, so no deadlock of this kind can occur. The rule costs nothing at run time, and it can be checked by inspection.

### Ordering in the face of choice

The difficulty arises when the set of latches is not known in advance. Transferring money between two accounts, for example, needs the latches of both, and which is lower depends on which accounts the customer chose. The solution is to derive the rank from something stable, such as the account number, and to sort the pair before taking either. Two transfers in opposite directions between the same accounts will then both take the lower account first, and they cannot embrace.

## Chapter 6: Hunting What Hides

Now suppose you have not been so careful, or someone before you was not, and a program fails once in a while for no apparent reason. This chapter is about the detective work.

### Why the failure is rare

A race fails only when the unlucky interleaving occurs, and that may demand a gap of a few instructions to coincide with a preemption. On a quiet machine with a light load the window is seldom hit. Under heavy load it is hit often. This is why so many faults appear first in production, where the traffic is far greater than anything the author's test could produce.

It follows that the printed output of a failing run is often useless. The program that crashed is not the program that was running, since the act of observing it changes the timing. A message written to the screen takes time and may accidentally serialise the threads at just the point where they would have collided. I have watched a bug vanish when a diagnostic line was added and reappear when it was removed. Such faults have been called by affectionate names, but they are not affectionate in the least.

### Provoking the failure

The most reliable method I know is to make the unlucky timing likely. Add a deliberate pause inside the gap you suspect, so that the other thread has ample time to intrude. If the program now fails every time, you have found the gap. Remove the pause once the repair is made, and then perform the experiment in reverse: with the repair in place and the pause restored, the failure should not appear.

Another helpful practice is to run the test with many more threads than the machine has processors. The scheduler then has more opportunities to preempt at awkward moments, and the rare interleaving becomes common.

### Reading the code with the lantern

Finally, the oldest method of all: read the code. For each piece of shared data, find every place it is touched and ask which latch is held at that place. A single access made without the latch is a candidate for the fault. This labour is tedious, and it is the labour that works. It is far cheaper to do it before the program runs than after the customers have complained.

## Chapter 7: Cost, Contention and Temperament

The last chapter gathers some matters that did not fit elsewhere. I begin with performance, since the objection I hear most often to the latch is that it is slow.

### What the latch really costs

Taking a free latch is cheap, comparable to a few ordinary instructions on most machines. The expense appears only when threads collide at the gate, and the more of them there are, the longer the line. This is *contention*, and it explains why a program can run quickly on two processors and slowly on eight: the extra threads spend their time waiting for one another.

If contention is a problem, the first remedy is the one already given: shorten the hold. The second is to split the data into independent parts, each with its own latch, so that unrelated work does not queue behind unrelated work. A table of customers might be divided into a number of sections, each with its own latch, so that two customers in different sections never meet. This is sound, but it multiplies the number of latches, and with the latches comes the need for the ordering rule of the fifth chapter. Do not divide the data until you have seen the line at the gate.

### Measure before you contrive

I have watched many people spend a month designing a clever arrangement to avoid a latch that cost nothing noticeable. Measure first. Find out whether the latch is in fact the slow part. If it is not, leave it alone, because a plain latch is easy to understand and easy to check, and those are properties worth more than a few percent.

### The temperament

I will end on temperament, which I think counts for more than technique. The person who writes good concurrent code is a little pessimistic. He assumes that whatever can interleave will, that the scheduler is hostile, and that his memory of which latch guards which datum is poorer than he thinks. He writes the pairing down, takes the latch for as short a time as he can, orders his latches before he takes two, and then reads his own work again with the lantern held low.

That is the whole of it. **One latch per piece of shared state, held briefly, never across a slow read, and taken in a fixed order when more than one is needed.** The rest is commentary, and I hope the commentary has been of use.
