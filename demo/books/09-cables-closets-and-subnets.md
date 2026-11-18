# Cables, Closets and Subnets

*by Garrick Pell, 2001*

## Chapter 1: Start with the Closet

Every small office has one: a cupboard, usually near the kitchen or behind the reception desk, in which the network lives. It holds a patch panel, a switch, a router, perhaps a modem, and a nest of cables that nobody has labeled. In my experience, more outages in small offices start in this cupboard than anywhere else, and most of them are caused by people, not equipment.

Begin, therefore, with the humblest and cheapest measure of all: keep the wiring closet *locked*. A hollow-core door and a ten-dollar lock will do. I have seen a cleaner unplug a switch to run a vacuum, a visitor lean a mop bucket against a patch panel, and a well-meaning manager push in a cable that "looked loose" and take down the accounting department. A locked closet removes these accidents. It also keeps casual hands away from the router's console port and the spare uplink, which matters for security as much as for tidiness.

Give the key to two people, not one and not ten. One person means that the whole office waits when she is on holiday; ten means that the lock is decoration. Keep a third key sealed in an envelope in the office safe, and write down who has been given a copy. When a staff member leaves, collect the key, and if you cannot be sure you have every copy, change the lock.

Inside the closet, label everything. Put a number on each end of each cable and the same number on the patch panel port and on the wall plate. Keep a simple chart of which port serves which desk. When something fails at four-thirty on a Friday, the label is the difference between a two-minute fix and an evening of tracing cables by pulling them.

Ventilate the cupboard, too. Switches and routers produce heat, and a shut door with no airflow turns a closet into an oven. A small fan, or a louvered door panel, costs very little. A locked and stifling closet is as bad as an open one.

## Chapter 2: Cabling That Lasts

For any office that will be in its premises more than a couple of years, run proper cable and do not depend on wireless alone. Twisted-pair copper cabling, of a category suited to gigabit speeds, serves nearly every small office well, and the limit for a single run between the switch and a wall plate is one hundred meters, including patch leads at both ends. Stay well inside that figure.

Keep network cable away from mains power wiring and from fluorescent light fittings, both of which can induce noise in the pair. Where the two must cross, let them cross at a right angle instead of running side by side. Do not bend the cable sharply, and do not pull it tight around corners: the twist of the pairs is what gives the cable its resistance to interference, and a kink or a staple through the sheath damages it.

Terminate the cable at a punch-down patch panel on the closet end and a socket on the desk end, and connect the two ends with short, factory-made patch leads instead of crimping your own. Home-crimped plugs are the most common source of intermittent faults I am ever called to repair. Test every run with a proper cable tester when it is installed, and record the result.

Install more outlets than you need. A room that has two desks today may hold four in three years. The labor of pulling cable is the expensive part, and the extra cable itself is cheap. A good rule is to double the number of desks you can see.

## Chapter 3: Addresses and the DHCP Server

Every device on a network needs an address, and in a small office the sensible way to hand them out is the *Dynamic Host Configuration Protocol*, or DHCP. A computer that joins the network broadcasts a request to anyone who will listen, a DHCP server answers with an offer containing an address, a subnet mask, a default gateway and the address of a name server, and the computer accepts. The address is *leased* for a period, often a day or a few days, and is renewed while the machine remains in use.

Choose a private address range for the office, and plan the pool. If your network is a single subnet of two hundred fifty-four usable addresses, you might reserve the lowest dozen for infrastructure such as the router and printers, give those devices fixed addresses or *reservations* tied to their hardware addresses, and let the remainder form the pool for laptops and phones.

The cardinal rule, and one of the commonest ways small offices come to grief, is: one DHCP server per network. The router usually provides one by default. Then someone plugs in a cheap wireless access point, from the home-electronics aisle, that also has a DHCP server switched on at the factory. Now two servers hear every request, and they *race* to answer. Whichever offer reaches the computer first is generally the one it takes. On Monday the laptop gets an address from the router and everything works. On Tuesday the same laptop hears the access point first, takes an address from an unrelated range, and discovers that it can reach neither the file server nor the internet.

The symptoms are maddening because they are intermittent: some machines work, some do not, and which ones change from day to day. If a machine reports an address you did not plan, look for a second server. Disconnect devices one at a time from the switch until the stray addresses stop. Then turn off the DHCP feature of the guilty device, and put the access point into bridge mode, so that it passes requests through instead of answering them.

Some managed switches offer a feature that blocks DHCP offers arriving on ports where no server is expected. If you own such a switch, use it.

## Chapter 4: Subnets and VLANs

As an office grows, one flat network becomes awkward. Guest laptops sit beside the payroll workstation, and the camera system chatters over the same wires as the file server. The tool for separating them is the *virtual LAN*, or VLAN: a way of telling a managed switch that certain ports belong to one logical network and others to another, even though everything plugs into the same box.

Give each VLAN its own subnet, with its own address range, and let the router connect them, passing only the traffic you permit. A typical small office might have one VLAN for staff, one for guests who need only internet access, and one for devices such as printers and door controllers. The router, or a firewall behind it, then decides what may cross. Guests may reach the internet but not the staff network. Staff may reach the printers. Printers reach nothing at all unless asked.

Each VLAN needs a DHCP scope of its own, and the same rule applies: one server per subnet, and make sure a scope in one VLAN cannot collide with the range used in another. Write the plan down: the VLAN number, its name, its subnet and mask, its gateway and its pool. A single sheet of paper pinned inside the locked closet door will save you hours.

Resist the temptation to build more than you need. A shop with eight employees does not need a dozen VLANs. Two or three, designed deliberately and documented well, will give you nearly all the benefit, and a small number of moving parts is easier to keep running when you are not there.

End each year with a walk-through of the closet. Check the labels, check the chart, test the backup of the router's configuration, and confirm that the key list is still accurate. It takes an hour, and it is the cheapest insurance you will ever buy.
