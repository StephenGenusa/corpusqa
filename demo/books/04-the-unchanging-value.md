# The Unchanging Value

*by Teodor Marsh, 2016*

## Chapter 1: A Number Is Not a Place

Start with an observation so obvious that we tend to skip it. The number seven cannot become eight. You can write seven on a page and cross it out and write eight, but you have altered the page, not the number. Seven remains seven in every conversation, on every machine, for as long as arithmetic exists.

Programmers learn early to treat variables as places, boxes that hold a value and can be overwritten, and the habit is so deep that we forget it was a choice. A box can be seen by many parties, and when one of them overwrites the contents, all the others see something they did not expect. Much of what makes concurrent programming a trade of anxiety comes from this single habit. This book proposes that we give it up wherever we reasonably can.

The idea has a name, *immutability*. An *immutable* value is one that, once created, can never be changed. Its fields are fixed at the moment of construction, and every later operation that appears to alter it produces a different value and leaves the original as it was. A text string in many systems behaves this way. So does a date, a point on a map, or a record of an order that has been placed.

My argument across these chapters is short and I will put it at once: **a value that never changes cannot be the subject of a race.** The reason is plain when stated carefully. A race condition requires at least two parties and at least one of them writing. Two threads that only read cannot disturb each other, however closely they overlap. If no thread can write, the whole problem is gone, and no discipline, no ordering rule and no clever machinery is needed to keep it gone.

I came to this view through a long, expensive education in the opposite style. For ten years I wrote programs in which objects were mutable by default, and I spent a considerable part of those years finding out why an object I had passed to a helper came back changed. Concurrency made this worse by a factor I could not have predicted. Single-threaded, the surprise is at least deterministic; I could rerun the program and watch it happen again. With threads, the surprise arrived once in a thousand runs.

The remaining chapters explain how to build real programs on this foundation, which is not trivial. Real programs do change over time, since a shop's stock goes down when a customer buys. I will argue that change can be modelled without mutation, that doing so has a cost and that the cost is smaller than people fear.

## Chapter 2: The Pricing Engine That Disagreed With Itself

Let me describe a failure that I watched develop. A retailer had a pricing engine, a service that computed the price of each item given a table of discount rules. The rules lived in a single object that a back-office tool updated several times an hour: the tool would remove a rule, add another, and adjust a percentage. Meanwhile, hundreds of request threads walked this object to calculate prices for shoppers.

Nothing was wrong most of the time. Then a customer was shown a basket total that was lower than the sum of its items' individual prices by a sum that no rule could explain. The engineers combed through the logic and found it correct. The fault was that a request thread had read half the rules from the old arrangement and half from the new. The back-office tool was in the middle of an edit, having removed the old seasonal discount and not yet added its replacement, when the request thread walked past. The price it computed corresponded to no state of the rules that had ever existed.

This is a more insidious fault than the classical lost update. No individual write was lost. Each read was individually valid. The trouble was that the reader saw a *torn* view: a collection observed partway through a change. In the vocabulary of the field, the operation was not atomic with respect to readers, and the reader had no way to know.

The engineers' first response was predictable. They put a lock around the rules, which they held while updating and while reading. This worked, in the sense that totals stopped being wrong, but the cost came due at the next sale. Every request now had to take the same lock to compute a price, and the lock became the narrowest point in the system. Throughput fell during exactly the hour when the retailer most needed it.

The second response, which I was invited to review, was to stop editing the rules. Instead, the back-office tool assembled a complete new rule table in private, where nobody could see it, and when it was finished it replaced the engine's reference to the table with a reference to the new one, in a single step. Requests that had begun with the old table finished with the old table. Requests that began afterward saw the new one. No request ever saw a mixture, because there was never a mixture to see. **Build the new value off to the side and swap it in whole; never edit a value that others can see.**

The pricing engine got faster, and I think this is the least appreciated consequence. The request threads no longer synchronized with anything. They loaded a reference, used the table it pointed to, and forgot it. The table could not change beneath them because nothing in the system was permitted to change it.

## Chapter 3: Replace, Don't Mutate

The technique in the pricing story generalizes into a single working rule: **replace, don't mutate.** When something about the world changes, do not alter the value that represents it; create a new value that represents the changed world, and let interested parties switch to it.

Notice what has happened to the notion of change. Time has become a sequence of values rather than a sequence of edits. Where the mutable programmer sees a bank balance that is now ninety and was a minute ago a hundred, the immutable programmer sees two values, a hundred and ninety, each perfectly stable, and a reference that has moved from one to the other. The reference is the only thing that moves. It is a small thing, a single word of memory, and it can be moved safely with one of the cheap primitive operations I will describe presently.

This is why I like to talk about *identity* and *state* as two distinct ideas. Identity is the thing we mean when we say "the pricing rules" or "the customer's basket": a name that persists through time. State is what that thing looks like at some instant. The mutable style fuses the two, so that the identity is the box and the state is whatever is currently inside. The immutable style separates them. The identity is the box, and each state placed in it is a value that is never itself changed.

There is a practical benefit that goes beyond races. Because old values are never destroyed by updates, they make wonderful snapshots. A report that must be consistent can simply hold on to a value and compute from it, for as long as it likes, while the live system moves on. Debugging gains a similar gift. If a customer complains about a basket total, the value that produced it can be kept and examined later, intact, because no later operation could have altered it.

Some care is needed at the edges. A value is immutable only if *everything* it contains is immutable. A record that holds a reference to a list that somebody can still modify is not safe, because the contents of the list are part of the value in any sense that matters. This is the *shallow immutability* trap, and it is the commonest way for well-meaning designs to leak. A reviewer should ask, of any type declared to be unchanging, whether its parts are unchanging too, all the way down.

A second edge concerns construction. An object under construction is not yet immutable, and if it is shared before it is finished, other threads may see it half-built. The standard remedy is to finish building before publishing: assemble the value fully, then make it available through the one swap of a reference. This is the discipline of the pricing story in miniature.

## Chapter 4: But Copying Is Slow

Every audience I have addressed raises the same objection at this point. If every change creates a new value, surely the program spends its life copying. A table with ten thousand rules cannot be duplicated to change one percentage. The objection is reasonable, and had immutability been limited to naive copying, I would not be writing this chapter.

The resolution is a family of structures designed for exactly this circumstance, called *persistent data structures*. A persistent structure is one in which an update leaves the old version intact and produces a new version cheaply, by sharing most of its internals with the old.

The easiest example is a singly linked list. To produce a new list that has one extra element at the front, you create a single new cell that points to the old list. The old list is untouched, and it is now a part of the new one. Nothing has been copied. Two values, differing by one element, share all the rest of their storage, and since neither can change, the sharing is perfectly safe.

Trees do something similar. Suppose the rules are held in a balanced tree of ten thousand entries. To change one entry, you create a new copy of that entry's leaf, then new copies of each node on the path from the leaf to the root, which is a short path, perhaps a dozen nodes. Every other branch of the tree is shared between the old and the new versions. The cost of an update is proportional to the depth of the tree and not to its size. This technique is called *path copying*, and with it a structure of millions of entries can be updated in a few dozen allocations.

I should be honest about the bill. Persistent structures use more memory per element than flat arrays, they scatter data in ways that make modern processors work harder, and reading one is generally a little slower than reading its mutable cousin. For the case of a flat sequence read in a tight loop, a plain array may beat them handily. **Persistent structures make copying cheap enough that you should default to them, and drop to mutable storage only where a measurement says you must.**

There is a further point in their favour that people overlook. Garbage collection and allocation have become fast, and the work that was once a heavy cost is now commonly dwarfed by the cost of the locks that the mutable alternative would have needed. In the pricing engine, replacing the table cost a few thousand allocations twice an hour. The lock it displaced cost every request, all day. That comparison is typical.

## Chapter 5: How Programs Change Their Minds

An objection of a different kind comes next. If nothing changes, how does the program do anything? Somewhere, surely, a counter must count and a stock level must fall.

The answer is that a program built on immutable values keeps a small number of mutable *references*, and the mutation is concentrated into them. A reference holds the current value of some identity. To change the world, a thread computes a new value from the old and then installs it.

The installation is the delicate step. If two threads each read the current value, each compute a successor, and each install theirs, then one successor overwrites the other and an update is lost. We have reconstructed the very race we were trying to avoid, but confined to a single word of memory. The cure is an operation that installs the new value only if the reference still holds the one that the thread started from, known as *compare-and-swap*. If the check fails, someone else got there first, and the thread simply recomputes its successor from the newer value and tries again. Because the successor is computed from an immutable value, recomputing costs nothing worse than time, and no partial state is ever visible.

Some systems offer this as a ready-made abstraction, a kind of cell whose update takes a function from old value to new, and retries automatically. I recommend such facilities strongly, because they place the retry logic where it can be written once and tested thoroughly. **Keep your mutable state down to a few references to immutable values, and update them with compare-and-swap or a library cell that does so for you.**

A warning about one small hazard. The function that computes the successor may run more than once, since a failed attempt is retried, so it must have no side effects. If the function sends an email, the customer may receive three. Compute in the function; act afterward, outside it.

Another, subtler habit also helps. Whenever possible, push the mutable reference to the edge of the program. The interior of the system, the pricing logic, the validation, the reports, should consist of functions that take values and return values. The edge, where requests arrive and results are stored, holds the few places where the identity of the world is moved from one value to the next. A program organized this way has a very small area in which timing can matter, and that area can be examined by a single reader in an afternoon.

I have seen teams adopt this shape one module at a time, starting with the pricing engine, then the session store, then the inventory. Each conversion removed a lock, and each removed lock removed a class of intermittent failure from their operations dashboards.

## Chapter 6: When Not to Be Pure

I do not want to leave the impression that every problem should be solved this way, and the honest portion of this book is the closing one.

There are circumstances where mutability is the right choice. A large buffer of audio samples, being filled by a hardware driver in real time, cannot sensibly be recreated with every sample. A matrix of ten million floating-point numbers, being multiplied by another, benefits enormously from in-place computation. In such cases the right defence is not immutability but *confinement*: let a single thread own the buffer for as long as the work lasts, and hand it on only when it is finished. A mutable value that only one thread can reach is as safe as an immutable one, and I have no quarrel with it.

A second exception is a hot counter incremented by every request. A persistent structure would be pointless here. A single atomic increment is the right tool, and the same advice applies to a flag that one thread sets and others read. These are modest instruments, to be used with the humility they deserve.

The third is performance, and I think the usual order of operations is correct. Write the program with immutable values. Measure it under realistic load. Where the profile shows that allocation or copying is a bottleneck, and not before, introduce mutation there, behind a narrow interface, and write down why. The cost of premature mutation is a lifetime of subtle bugs, while the cost of premature immutability is an afternoon with a profiler.

My final recommendation is the simplest and the one I most hope readers will carry away. **Share freely, but only what cannot change.** If you must change something, change a reference to a new value and not the value itself. If you cannot do that, make sure exactly one thread can see the thing you are changing.

A colleague of mine, who maintained a very large system for years, once told me that the most calming sentence he knew was "that object is never modified". It meant that he could stop worrying about a whole region of the program. Every programmer ought to want as many such regions as she can get, and the work of this book has been to describe how to make them.
