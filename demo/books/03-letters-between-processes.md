# Letters Between Processes

*by Ingrid Vasquez-Holm, 2011*

## Chapter 1: The Post Office and the Whiteboard

I want to begin with two pictures of how people cooperate, because most of what follows is an argument about which picture a programmer should carry in her head.

In the first picture, a group of colleagues shares a whiteboard. Anyone may walk up and erase a figure, write a new one, or cross something out. The board is always current, and everyone can see everything at a glance. It is also, anyone who has worked in such an office will tell you, a place where numbers go missing. Two people reach the board at once, each rubs out the same total, each writes a different replacement, and the one who wrote second never learns that the first had anything to say.

In the second picture, the colleagues sit in separate rooms. Each keeps her own ledger and nobody else may touch it. When one needs something from another, she writes a note and sends it down the corridor. The recipient reads her notes in the order they arrive, one at a time, and acts on them in her own room. Nothing is ever visible to two people at once, because nothing is ever in two rooms except as a copy.

The first picture is *shared memory*, and for most of the history of computing it was the default way to make a program do several things at once. The second is *message passing*, an older idea than most people suppose. Researchers in the early decades of the field were already sketching systems in which independent processes communicated only by sending each other discrete packets of data. What changed in the last few years is not the idea but the pressure. Machines now arrive with many cores, and the ordinary application programmer, who never needed to think about simultaneity, is expected to use all of them.

My own education in the matter came from a small relay service I maintained for a few years, a program that accepted connections from chat clients and forwarded each line of text to everyone else in the same room. It was written the way I had been taught. A table of rooms lived in memory, each room held a list of connected users, and each connection was handled by its own thread that reached into the table whenever it needed it. For a long time it worked. This book begins where it stopped working, and it is a long argument that I should not have been surprised.

I will say plainly what the argument is. My claim is that the trouble with the whiteboard is not carelessness. The trouble is the whiteboard. A design in which many threads can reach the same mutable data must be defended against every interleaving of their steps, and the number of interleavings grows faster than any person can enumerate. A design in which only one thread can reach each piece of data has nothing to defend. The rest of these chapters try to show how to build programs of the second kind and what it costs.

## Chapter 2: The Night the Room Count Went Wrong

The failure in the relay was small, and that is why it taught me so much. Each room kept a count of how many users were present, displayed beside the room's name in the client. One evening the count for a popular room read minus two. A day later another room claimed to hold users when it was plainly empty.

The cause was an instance of what the literature calls a *race condition*: the result of the program depends on the relative timing of events that nobody controls. Here the offending sequence was the ordinary, innocent one. To admit a user, a thread read the current count, added one to it, and wrote the answer back. Three small steps, and between any two of them another thread was free to run. If two users joined within the same instant, both threads could read the number seven, both could compute eight, and both could write eight. Two people had arrived; the count had risen once. The mirror image, with departures, produced the negative figures. Programmers call this shape a *read-modify-write*, and it is the most common way that shared data goes bad.

What I want to stress is how unremarkable the code looked. There was no exotic construct, nothing a reviewer would have flagged. The line that updated the count read like arithmetic, and arithmetic is the thing we trust most. The program was wrong only on a timescale of nanoseconds, and only under load, so it passed every test I wrote on a quiet machine.

The bug also revealed something about the diagnosis. I could not reproduce it on demand. I added logging, and the logging, by slowing the threads, made the problem vanish. This is a well-known and maddening property of such faults, sometimes called a Heisenbug in the trade slang: observing the program changes the timing and hides the defect. A fault you cannot reproduce is a fault you cannot confidently claim to have fixed, and a fix you cannot verify is a hope.

I spent a week on that count. When I was done I had not merely repaired it; I had stopped trusting my own mental model of the program. I realized that I had been reasoning about the service as if it did one thing at a time, and the service had never done any such thing. Every line I read, I read as though no other line were running. The machine disagreed.

It is tempting at this point to reach for the usual remedy, and many readers will have already thought of it. I will deal with that remedy fairly in a later chapter. For now I only want to record that the remedy treats the symptom of one particular count. My question was larger: was there a way of building the relay so that this whole class of fault could not be written down at all?

## Chapter 3: Every Process Keeps Its Own House

The answer I found was to change who is allowed to touch what. In the redesigned relay, each room became an independent unit of computation, which I will call an *actor* following the vocabulary that has grown up around the idea. An actor has private data, a way of receiving messages, and the ability to send messages to other actors it knows about. That is all. It has no door through which another actor can reach into its data.

The first principle of the design is **ownership**. Every piece of mutable data belongs to exactly one actor, and only that actor ever reads or changes it. The room's count, its member list and its recent history were all inside the room actor. When a connection wanted to join a room, it did not touch the count. It sent the room a message saying, in effect, "please admit me", and the room did the arithmetic in its own house.

The second principle follows directly. If you want to give someone information, give it as a copy. A message carries a *value*, a snapshot of data as it stood when the message was written, and once sent it no longer belongs to the sender in any way that matters. **Let each worker own its data and send copies, never references.** A reference to an actor's internal list, passed out in a message, quietly rebuilds the whiteboard: now two parties hold the same object and either might change it. Many message-passing designs fail in precisely this way, by passing a pointer where they should have passed a letter. When a message is large, there are ways to avoid the cost of duplicating it, and I will discuss them below, but each of those ways must preserve the illusion that the receiver holds the only copy, or that nobody holds a copy that can change.

I find that the discipline sounds restrictive and feels liberating. Once I had settled on ownership, whole categories of question disappeared. I no longer asked "who else might be modifying this?" because the answer was always nobody. I no longer asked "what order do these two steps run in?" because within one actor there is one sequence of steps, written down in the order I wrote it.

It is worth being clear about what this does and does not claim. The principle does not say that programs become simple. It says that the *difficulty is relocated*. Instead of the intricate question of how threads interleave their access to common data, you face the design question of how to divide the data among owners and what messages the owners must exchange. That is a question about structure, and structure is something people are good at. I would rather spend my effort on a diagram than on a proof about instruction ordering.

A useful test when reviewing such a design is to pick any variable and ask who owns it. If the answer is a single name, the variable is safe. If the answer is "well, several of them, but they take turns", you have found a place where the discipline has slipped and a race may be waiting.

## Chapter 4: The Mailbox

How does a room actor, which may have thousands of connections addressing it at once, avoid the very collision we started with? The answer is the humblest piece of machinery in the design: the *mailbox*.

Each actor has a queue of incoming messages. Senders append to it and the actor removes messages from the front, one at a time. The actor processes a message completely, finishing every change it intends to make, before it looks at the next. Because the actor's own data is touched only while it handles a message, and only one message is handled at a time, the data is never touched by two things at once. **A mailbox serializes access, so the actor needs no lock around its own state.**

Consider the count again. A hundred users join in the same instant. A hundred messages land in the room's mailbox in some order. The room takes the first, raises the count from seven to eight, and finishes. It takes the second, raises eight to nine. The result is a hundred and seven, every time, whatever order the arrivals took. The read-modify-write that destroyed my original service is still there, but it can no longer be interrupted, because the only agent that can interrupt it is the actor itself, and the actor is busy.

Some readers will object that the mailbox must itself be shared: many senders append to one queue, and surely that is a race. It is a fair point and I do not dismiss it. The queue is indeed touched by many parties, and somebody must make its operations safe. The answer is that this is done once, inside the machinery that implements mailboxes, by people who specialize in it, and not again in every application that uses them. The application programmer never writes the dangerous code. Her program sees only the guarantee: messages arrive whole, and they are handled one at a time.

There is a second guarantee that people sometimes assume and should not. Between a single sender and a single receiver, messages commonly arrive in the order they were sent. Between different senders, there is no such promise, because the paths they take can differ in length. If your logic depends on message A from one actor arriving before message B from another, you have invented a race of your own, only one level up. Design so that the receiver can tolerate either order, or have the receiver ask for what it needs.

I have come to think of the mailbox as the programmer's answer to the receptionist. A receptionist does not make the executives less busy, and does not make the building more spacious. She makes sure that people come in one at a time.

## Chapter 5: What the Post Office Costs

I would be a poor guide if I only described the pleasant parts of the country. Message passing has real costs, and a book that hid them would send its readers home to be disappointed.

The first cost is *latency*. Asking an actor a question and awaiting its reply takes longer than reading a variable. For a handful of operations per second this is invisible. For an inner loop that does millions of reads, it may be ruinous, and in that setting a different design is appropriate. I do not claim the post office is the right tool for every errand.

The second is *unbounded queues*. A mailbox that accepts messages without limit will accept them faster than the actor can process them, if the senders are quick and the receiver is slow. The queue grows, memory fills, and the service dies of politeness. Every serious design needs some means of *backpressure*, a way for a full mailbox to make its senders wait or drop work. Choosing which is a decision about your product and not merely about your code.

The third is that deadlock does not disappear; it changes costume. Two actors that each wait for a reply from the other, before they will handle any further message, will wait forever. No lock is involved, but the symptom is identical. The usual cure is to avoid blocking on replies at all: send a request, carry on, and handle the answer as one more message when it comes. An actor that never stops listening cannot be wedged this way.

The fourth is *testing*. Because ordering across senders is not fixed, a test that passes today may pass by accident. I have found it valuable to build test harnesses that deliver messages in deliberately shuffled orders and check that the actor's state comes out right regardless. This is far easier than hunting a timing bug in shared memory, because the shuffle is explicit and repeatable, but it is work that must be done.

The fifth is *distribution*. When actors live on different machines, a message may be lost, duplicated or delayed. The model extends to this case with care, since an actor was never entitled to assume that its correspondent was close by, but the programmer must now decide what to do about a letter that never arrives. I mention it so that nobody imagines that the metaphor of the post makes the postal system reliable.

None of these costs is an argument for returning to the whiteboard. Each is a cost of a kind that can be examined, measured and designed around, and none involves a failure that appears at random once a month at three in the morning. I will accept a predictable difficulty over an unpredictable one nearly every time.

## Chapter 6: Moving House Without Burning It Down

Few readers get to start a program from nothing. Most inherit a thing with a table of rooms in shared memory and a hundred places that reach into it. The question is how to move toward the quieter design without a rewrite that never ships.

My advice is to find the seams. Look for a piece of mutable data that many threads touch and that has a clear identity, such as the room table, a session store, or the account of a single customer. Make it the property of one actor, and replace each of the direct accesses with a message. Do this for one piece of data at a time. At every step, the program is still correct, because each step removes sharing rather than adding it.

During the transition you will often have to live with both styles at once. Be strict about the border. **Never let a reference to an owned structure leave its owner; hand out a copy, or a read-only snapshot, instead.** I keep a rule on my own team that any code review which finds an internal list returned from an actor's method is sent back, regardless of how convenient it seemed.

Do not turn every object into an actor. A common beginner's error is to create a tiny actor for each small thing, so that the program becomes a dense web of messages in which nobody can follow the flow. Group data by how it changes together. If two values must always be updated as a pair, they belong with the same owner.

In a few places you may find that a single shared structure truly is the right answer, a counter read by monitoring code, say, or a cache of expensive results. I do not forbid it. I ask only that it be rare, deliberate, and labelled. **Share mutable state only where you can name the exact reason you cannot avoid it.** Everywhere else, prefer to give a piece of work a single home and let everyone who needs it write to the door.

Today the relay I started with is nine actors' worth of code, and it has not produced a wrong count since the move. I cannot prove it never will. I can say that when I read it, I read each part as it is: one room, one ledger, one letter at a time. It is, to my surprise, the only concurrent program I have ever understood completely.
