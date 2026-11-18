# Ledgers in the Machine

*by Philippa Strand, 2005*

## Chapter 1: The Spreadsheet as a Ledger

A century ago a small firm kept its accounts in a bound ledger, and the ledger had a keeper. Entries were written in ink, in a fixed order, by one person who answered for them. When a figure was wrong, the page showed who had written it and when. The spreadsheet has replaced the bound ledger in nearly every office I visit, and in the move the keeper has disappeared. A file on a shared drive can be opened by anyone, changed by anyone, and saved over by anyone, and when a figure is wrong no page remembers whose hand put it there.

This book is a set of working habits for putting the keeper back. None of the habits requires a clever formula or special training. They require only that you decide, in advance, who is responsible for what, and then use the features the software provides to hold everyone to the decision.

A well-built workbook has three kinds of cells, and every habit in this book follows from keeping them apart. *Inputs* are figures typed by a person: the price list, the opening balance, the tax rate. *Calculations* are cells that work out a result from other cells, and no person should type over them. *Outputs* are the summaries that go to the partners or the bank. Mixing these in a single block, with a typed number sitting quietly in the middle of a column of formulas, is the single most common source of the errors I am asked to find.

Give each kind a visible look. Many firms shade input cells pale yellow, leave calculations unshaded, and put outputs in a bordered box. The convention does not matter; the consistency does. A newcomer should be able to tell at a glance which cells she may change.

## Chapter 2: Locking Cells and Protecting Sheets

Spreadsheet programs let you *lock* cells, and the feature is misunderstood so widely that I will explain it slowly. Every cell has a setting called locked, which is on by default. The setting does nothing by itself. It takes effect only when you also switch on *sheet protection*, which then forbids editing of every locked cell. So the procedure is the reverse of what newcomers expect: first unlock the cells that people ought to change, which are the inputs, and then protect the sheet, so that everything else, the calculations and outputs, is held fast.

You may attach a password to the protection. Use one, but treat it as a reminder and not a safeguard. The protection in most spreadsheet programs is meant to prevent accidents, not attacks, and a determined person can remove it. Its job is to stop a tired clerk from typing a number over a formula on a Friday afternoon, and it does that job well. Record the password in the firm's sealed record of passwords, and name one person who holds it.

Protect the structure of the workbook as well. This option stops people from adding, deleting, renaming or hiding the sheets, which matters because a formula that refers to a sheet by name breaks when the name changes. Hidden sheets deserve special suspicion. I once spent a morning following a total to a hidden sheet that held a year-old estimate, silently feeding the figures for the current quarter.

Lock the cells, protect the sheets, and then test the protection by trying to type over a formula. If it lets you, the protection is not on.

## Chapter 3: When Two People Edit the Same Sheet

Now I come to the trouble that sends most firms to a book like this one. Two staff members open the same workbook, the same morning, and each changes a few figures. They each save. Which version survives?

The answer depends on how the file is shared. If it sits on a shared drive and is simply opened by two people, it becomes a race to save first: the second to save generally wins outright and the first person's work is gone, or else the program refuses to open the file for the second person, and offers a read-only copy. Neither result is what anyone wanted. In either case the problem is not a technical fault. Two people each did something sensible, and the difference between their work was never reconciled. It is a *business-process conflict*, and it is best solved by procedure.

Older spreadsheet programs offered a *shared workbook* feature, in which several people open a file together and the program merges the changes when each person saves. It works for simple edits, such as typing figures into different rows. When two people change the same cell, the program asks the later saver to choose which value to keep. It does not cope well with edits to structure, such as inserting columns, and a heavily shared file tends to grow slow and sometimes corrupt. Use it only for a short period and a limited purpose, and keep a backup before you start.

The more dependable cure is the old one: give every sheet an *owner*. One person alone is responsible for each sheet and the only one permitted to alter it. The others may read it, copy figures from it, and send the owner a note. Write the owners in a small table on the first sheet of the workbook, with the date of each person's last change. When the sales sheet is the sales manager's, and the payroll sheet the bookkeeper's, two people can never overwrite one another by accident, because they are never working on the same sheet.

Where a sheet really must be touched by several people, set up a booking routine. A paper sign-out sheet by the manager's desk works as well as anything electronic: write your name and the time when you take the workbook, and cross it out when you return it. Save under a new name with the date each time, so that a mistake can always be unwound.

## Chapter 4: Habits That Keep the Ledger Honest

A few small habits, taken together, will prevent most of the troubles in this book.

Keep one master copy and know where it is. Name copies by date and keep the old ones for a defined period, perhaps a year. Disk space costs very little next to the cost of a lost month.

Before any large change, save a copy and note what you are about to do. After the change, compare the totals. A good workbook has a *check cell* that adds a column two ways and reports the difference, which should be zero. Make the check cell large and red when it is not.

Never hand-type over a calculation. If a formula gives the wrong result, fix the formula, or fix the input it uses. A figure that has been overtyped looks like every other figure, and it will give a wrong answer again, silently, the day the inputs change.

Review the workbook on a schedule. Once a quarter, the owner of each sheet should look it over, confirm the inputs against their sources, and sign the first sheet. A ledger without a keeper decays. A ledger with a keeper, even one who spends only an hour a quarter on it, lasts for years.

One last practice deserves a place here, and it concerns the quiet moments between edits. When a person takes over a sheet from a colleague, whether through holiday, illness or a change of role, the handover should be written. The outgoing owner lists the inputs that need monthly attention, any figures that depend on outside sources, and the known oddities that every long-lived workbook accumulates. The incoming owner reads the list, asks questions, and signs the table on the first sheet. A ten-minute conversation at that moment prevents the slow drift in which no one is quite sure who has been looking after the *tax* sheet since March, and everyone assumes someone else has.
