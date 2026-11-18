# Field Notes from the Scheduler

*by Hollis Penwarden, 1989*

## Chapter 1: How I Came to Watch the Clock

I did not set out to become a person who worries about the order in which things happen. I was hired, in the spring of my twenty-sixth year, to keep a mid-sized machine running for a firm that ordered, stored and shipped bathroom fittings, and for the first two years the work was honest and dull. Tapes were mounted and dismounted. Reports were printed. Now and then a disk filled up and had to be coaxed back to health.

Then the firm bought a new operating system, one that could keep several programs alive at once and switch among them on the strength of a clock. The salesman had been very pleased with it. He explained that nothing need ever sit idle again. I remember nodding, and I remember a small cold thought that I did not voice: that when many things happen at once, someone must decide which happens first, and that someone would not be me.

These notes are what I learned over the years that followed. They are not a textbook. I have not the training for one, and the books I have seen are written in a language I find hard to follow. They are field notes, the sort a surveyor keeps, setting down what the ground was actually like where he walked. I have changed the names of the departments and left the rest.

### The scheduler

A word on the scheduler, since it is the main character. It is the part of the operating system that decides which program gets the processor, and for how long. Every few thousandths of a second a clock fires, the running program is stopped wherever it happens to be, and the scheduler picks another. The stopped program is not told. It simply finds, when its turn returns, that the world may have moved.

For a program working entirely on its own data this does no harm. The trouble begins when two programs, or two *tasks* within one program, handle the same piece of data. Then the scheduler's choice of stopping place becomes a matter of consequence, and the scheduler, having no idea what the data means, chooses without regard to it.

## Chapter 2: The Case of the Missing Deposit

The first trouble I met was a small one, which is why I remember it so clearly. It cost the firm about forty dollars a week, and it took me three months to find.

The firm kept an account for each of its trade customers, and it posted payments to these accounts through a program that ran all day, taking in cheques as clerks keyed them. Two clerks worked at once, each with a terminal, and the program served both by running a task for each. At the close of every week, the accountant would reconcile the posted payments against the bank, and every week the books came out short by a figure that wandered between ten dollars and a hundred. The accountant blamed the clerks. The clerks blamed the accountant. Nobody blamed the scheduler, because nobody had heard of it.

### What the program did

When I finally read the posting code, it was a model of clarity. To post a payment, it read the customer's balance from a record in shared memory, subtracted the amount of the payment, and wrote the new balance back. Three lines, in the plainest possible style. I read it twice and could find nothing wrong with it, which is exactly what was wrong with it.

I sat with a pencil and drew two columns, one for each clerk's task, and I walked the clock through them. Suppose a customer owes two hundred. The first clerk keys a payment of fifty, and his task reads the balance and gets two hundred. At that moment the clock fires. The scheduler stops the first task and starts the second, whose clerk has just keyed a payment of thirty from the same customer, a husband and wife paying on different days that happened to meet. The second task reads the balance and also gets two hundred, subtracts thirty, and writes one hundred seventy. The scheduler returns to the first task, which, knowing nothing of what happened, subtracts fifty from the two hundred it remembers and writes one hundred fifty. The thirty-dollar payment has been erased as if it never happened.

### Why it was so rare

It happened rarely because the window was tiny. The gap between the read and the write was a few instructions, and the clock had to fire in that gap and the other clerk had to be handling the same customer. On most weeks nothing collided. When a collision came it left no trace. There was no error, no message, no corrupted record, only a balance that was a little wrong in the customer's favour, or the firm's.

The textbooks give this a name, a *lost update*, and the general class a name too: a *race condition*. I did not know the words then. I knew only that I had watched a payment disappear on paper, and that no amount of staring at the program would have found it, since the program was correct in every line.

### The remedy

The fix, once understood, was a small one. I put a lock around the balance. A *lock*, for those who have not met one, is a flag in shared memory that a task sets before touching some data and clears after. The flag is tested and set in one indivisible instruction, so two tasks cannot both find it clear; if it is already set, the task waits until it is clear. With the lock in place, the first clerk's task sets the flag, reads, subtracts and writes, and clears the flag. The second clerk's task, finding the flag set, waits a moment and then reads the corrected balance.

The rule I took away is a plain one. **Put a lock around any balance, count or total that more than one task can change, and hold it across the whole read, change and write.** It is not enough to lock the read, and it is not enough to lock the write. The lock must span the whole of the delicate operation, so that no other task can come between its parts.

The weekly shortfall vanished. The accountant was relieved, the clerks were vindicated, and I was left with the beginnings of a habit.

## Chapter 3: A Short Tour of the Clock's Habits

Before going further I should say something about how the scheduler behaves, because understanding its habits is what keeps a person out of trouble. I learned these by watching, and some I learned by being embarrassed.

### Time slices and the unlucky moment

Each program gets a *time slice*, a short stretch of processor time, and when it ends the program is set aside. The length of the slice is a compromise. Too long and the machine feels sluggish to those at the terminals; too short and it wastes its effort on switching. Whatever the length, the end of the slice can arrive at any instruction, and the program cannot choose.

Some programs give up the processor on their own, when they ask for a slow device and must wait. This is why trouble tends to cluster around input and output: a task that has asked for a disk read will be set aside for a long time, and other tasks will take the whole stretch to change whatever they like.

### The matter of priority

Most systems let you give some tasks a higher priority, so that they run in preference to others. This is useful for tasks that must respond quickly, such as the one that serves the terminals. It is also a fertile source of mischief. A high-priority task that waits for something held by a low-priority one can wait much longer than anyone expects, because the low-priority task seldom gets to run. We fought this at the firm for a good while before we understood it, and the cure was to take care in giving priorities at all. A modest, even arrangement serves better than a clever one.

### Why testing does not help

I have been asked many times why we do not simply test the programs more. My answer is that a test shows only the timings that happened during the test. A program that fails once in ten thousand runs will pass a hundred test runs and nobody will be the wiser. The clock does not fire in the same place twice, and one cannot ask it to.

What does help is to read the code and ask, for each piece of shared data, which tasks touch it and what protects it. I keep a notebook for this. One line to a datum, with the lock that guards it written beside. Where the space beside is empty, I go and look more closely.

## Chapter 4: The Print Spooler That Stopped

The second affair was a larger one and it taught me a lesson the first had not. It also nearly cost me the goodwill of an entire building.

### The setup

The firm had two printers on the shop floor, a fast one for invoices and a slow wide-carriage one for packing lists, and a *spooler*, a program that took print jobs from the other programs, queued them, and fed them to the devices. Some jobs needed both printers, for an order that required an invoice and a packing list produced as a matched pair. To manage this the spooler held a lock for each printer. A task that wished to print a matched pair took the lock on the invoice printer, then the lock on the packing printer, printed both, and released both.

This worked for months. Then, one Monday morning in the busy season, the printing stopped. Not slowly. It simply stopped, with a stack of orders waiting and the foreman standing at my door.

### What had happened

I found, after an hour of looking at the state of the spooler, that two tasks were both stuck. One was a matched-pair job from the despatch office, which had taken the lock on the invoice printer and was waiting for the lock on the packing printer. The other was a different job, written by someone else, that handled returns. It printed the packing slip first and the credit note second, and so it had taken the lock on the packing printer and was waiting for the invoice printer.

Each held what the other wanted. Neither could go forward, and neither could be made to go back. They would have sat there until the building fell down. The textbooks call this a *deadlock*, and the term has always seemed to me too gentle. It is a standoff in which both parties are entirely reasonable and entirely stuck.

### Why it hid

It had hidden for months because the two jobs needed to start within a few instructions of one another, each taking its first lock before the other asked for it. In the quiet season they seldom overlapped. In the busy season the overlap came in a matter of days. Like the lost deposit, the fault was in a program that was correct in every individual part.

### The fix

I could have forced the stuck job to give up, and for the morning I did. But the permanent cure was a rule. **Always take locks in one fixed order.** I decided that the invoice printer's lock would always come first and the packing printer's lock second, for every task in every program. The returns job was altered to take the invoice printer first, even though it would print on the other printer first. With the rule obeyed everywhere, the two jobs could no longer hold opposite ends of the same stick.

The reason it works is simple enough to draw on the back of a pay envelope. For a deadlock, there must be a ring of tasks, each waiting on the next. If every task takes the locks in the same order, no task ever holds a later lock while asking for an earlier one, and a ring cannot close. I pinned the rule to the wall of the machine room and wrote it into the first page of the programming notes, and I have kept to it ever since.

### Two lessons

The first lesson is that a lock does not cure everything; a lock introduces a hazard of its own. A person who adds locks to fix a lost update may later find that the locks themselves have bound up the machine. The second is that the rule must be known to everyone who writes code, including those who join later. The returns job was written by someone who had never been told of any order, and could not be blamed for it.

## Chapter 5: Habits That Keep a Machine Out of Trouble

A number of small habits grew out of those two affairs. I set them out here without ceremony, since none of them is difficult and all of them are neglected.

### Know what is shared

The first habit is to know which data is shared. Data that belongs to one task alone cannot be raced. Data that more than one task can reach is another matter, and the list should be kept short and written down. When a new piece of data is added to the shared region, I ask whether it needs to be there. Much trouble could be avoided if people kept their data private to begin with.

### Keep the lock brief

The second habit concerns how long a lock is held. **Release the lock as soon as the delicate operation is done.** In the early days I locked whole routines for the sake of safety, and the machine slowed to a crawl, with tasks queuing for the lock like customers at a bakery on Saturday. A lock should cover the few instructions that must not be divided, and no more. In particular, do not hold a lock while waiting for a tape or a printer; the wait may last seconds, and every other task wanting that lock is made to wait with you.

### Lock the data, not the routine

The third habit is subtler. A lock ought to belong to a piece of data, and be taken by whatever routine touches that data. I once found a program that had a lock for the posting routine and another for the reporting routine, the idea being that each routine could run only once at a time. But the two routines both touched the same balances, and so the two locks protected nothing. A lock named after a routine rather than a datum is a warning sign.

### Write the order down

The fourth habit has already been told: when more than one lock is needed, take them in a single fixed order, and write the order where all can read it.

### Distrust the clever

I have been shown, over the years, a number of devices for avoiding locks altogether. One man showed me a scheme of flags and turns that was supposed to need no lock, and I admired it for a morning. A week later I found a case in which it failed. I now say, to anyone who will listen, that a plain lock, correctly placed, is worth more than a clever scheme that nobody but the author understands. The author, too, forgets.

## Chapter 6: Looking Back at the Floor

I retired from the firm some years ago, and the machine that I tended has been replaced by one that I could not have understood even in my best year. The scheduler has grown more subtle and the hardware has grown faster. Yet I notice, when I read what young people write about their trials, that the faults are the old faults. Someone has lost an update. Someone has made two tasks wait on one another. The wrapping is new and the contents are the same.

### What I would tell a beginner

If I were asked to advise a beginner, I would say three things.

First, assume that the clock will fire at the worst possible instruction, because over enough time it will. Do not tell yourself that the gap is too small to matter. The gap is small and the time is long.

Second, find the data that more than one task can change, and put a lock around it, holding the lock across the whole of each delicate operation and releasing it promptly. Nothing else I know has prevented as many errors.

Third, if you must take more than one lock, choose an order, write it down, and never depart from it. A fixed lock order is the surest defence I know against the standoff that stopped my spooler.

### On the human side

I should add something about people, since the machine is only half of the story. The two faults I have described were not the result of stupidity. The author of the posting code was a careful man, and the author of the returns job was a conscientious one. Both made errors that anyone would make who had not been shown the danger. The best defence in the end was not a technique but a practice of telling each other what we knew: the notebook of locks, the rule on the wall, the habit of asking a colleague to read one's code with a hostile eye.

I think of the foreman standing at my door that Monday, a good man who wanted only to ship his orders, and I think how little he asked and how much was needed behind it. The scheduler does not care about orders, or customers, or fittings for bathrooms. It cares for the clock, and nothing else. It is up to us to put the locks where the clock cannot reach, and to take them in an order that cannot quarrel.

I have kept my notes on a shelf above my desk, and they now run to several volumes. This is the shortest summary I can offer of all of them: lock the data, keep the lock brief, and take your locks in the same order every time.
