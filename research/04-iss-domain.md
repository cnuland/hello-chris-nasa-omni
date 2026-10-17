# 04 - ISS Domain Research: Inventory, Prior AI/Vision Work, Constraints, Policy, Data

*Research date: 2026-10-17. Scope: domain grounding for a proof-of-concept that tracks objects and people aboard the ISS from live video and audio, keeps an inventory database, and shows mission control "items last seen" with location, who moved the item, confidence, and a snapshot.*

**Labeling convention.** **[V]** means verified: stated directly in the cited source, which I read (for PDFs, I extracted and read the text). **[I]** means inference: my reasoning or synthesis, not stated by a source. Some secondary sources are flagged as such. "NTRS" means NASA Technical Reports Server citation ID.

---

## 1. How ISS inventory is managed today

### 1.1 The Inventory Management System (IMS) and barcodes

- **[V]** U.S. ISS hardware is tracked in a barcode-based **Inventory Management System (IMS)** database. Every commodity is barcoded before launch. Its attributes are entered at that point: official name, part number, serial number, owner (NASA, Russia, etc.), barcode number, mass, dimensions, and category (Maintenance, Crew Health Care, EVA, and so on). An initial location is required. (Shull, Gralla, Silver, de Weck, *Logistics Information Systems for Human Space Exploration*, AIAA SpaceOps 2006-5733, http://strategic.mit.edu/spacelogistics/pdf/AIAA-2006-5733-554.pdf)
- **[V]** On orbit, the crew uses handheld barcode readers. **All approved stowage locations carry a barcode**, so a crew member scans the location, scans the items, and selects "move". The IMS can also be updated by hand on station laptops. Most crews let ground controllers do most of the updates. In MCC-Houston the **ISO (Inventory and Stowage Officer)** console owns daily inventory, and the Russian counterpart in MCC-Moscow is the **RISS**. IMS updates from the control centers and from orbit are **synchronized daily while the crew sleeps**. (same source)
- **[V]** Many consumables (food, clothing, office supplies) are barcoded **only at the bag level**. Contents are resupplied based on assumed usage rates, which leads to discrepancies and tedious crew audits. (same source)
- **[V]** The IMS keeps a **movement history** for each item. The first step in finding a lost item is to check the last few locations in that history. (same source)
- **[V]** IMS labels use **Code 39** barcodes and are required on all stowage containers: racks, lockers, trays, bags, refrigerators and freezers, and glove boxes. (SSP 50007 Rev B, *Space Station Inventory Management System Bar Code Label Requirements and Specification*, https://metalphoto.com/wp-content/uploads/downloads/2015/03/SSP50007RB.pdf)
- **[V]** The MSFC Payload Operations Integration Center stowage team (12 people) tracks science items, including tools, power cables, and trash. Their database records date, time, and the last person to use each item. Some items take **days or weeks** to find. The team keeps a "lost in space" database and "Wanted" posters. **Team members sometimes spot items drifting past station cameras during live video and relay the location to the crew.** (NASA, *From Tools to Trash: Marshall's Payload Stowage Team Tracks It*, https://www.nasa.gov/missions/station/from-tools-to-trash-marshalls-payload-stowage-team-tracks-it/)
  - **[I]** That last point is direct operational precedent for this POC. People in mission control already use the live video feeds informally as a lost-and-found sensor.

### 1.2 Scale, lost items, and crew time

| Metric | Value | Source |
|---|---|---|
| Items in IMS (2006) | ~20,000 U.S. and Russian items, ~3% (~600) "lost" | AIAA 2006-5733 [V] |
| Items in IMS (older press) | 22,000 items, 638 lost | Air & Space, *Lost in Space* (secondary) |
| Items in IMS (2010s) | ~130,000 tracked, 64,000 active, ~3,000 lost | Harvard LISH, https://aiinstitute.hbs.edu/lost-in-space-can-the-crowd-help-nasa-find-items-on-the-international-space-station/ [V] |
| U.S. stowed and loose items | "almost 10,000" | Brown et al., *Asset Tracking on the ISS Using Global SAW Tag RFID*, https://rfsaw.com/wp-content/uploads/2017/12/paper-asset-tracking-on-the-international-space-station-using-global-saw-tag-rfid-technolgy.pdf; NTRS 20070020337 [V] |
| Scheduled IMS upkeep | 20 min per crew member per day ("in reality a much larger portion") | AIAA 2006-5733; Brown et al. [V] |
| Searching for items | **~34 crew-hours per year**, plus ~13 h/yr for periodic inventories | Ewert & Broyan, *Improving Logistics and Waste Management for Deep Space Human Exploration*, NTRS 20180007471 [V] |
| One audit (Apr 2007) | >3 hours of whole-crew time | 2007 NASA/RF SAW paper (via search summary) |
| Request to located | "hours to days"; "on the order of days without localization automation" | NASA, *Spacecraft Autonomous Inventory Tracking* spec sheet (2024), https://www.nasa.gov/wp-content/uploads/2024/09/8-inventory-tracking-spec-sheet-508.pdf [V] |
| Value of crew time | $130,000 per crew hour (NASA commercial price list, Feb 2021) | SpaceNews and eoPortal, https://directory.eoportal.org/other-space-activities/iss-pricing [V, secondary] |

- **[V]** Anecdote: the first tomato grown on the ISS went missing for about 8 months (2023). Frank Rubio said he spent "eight to 20 hours" of his own free time looking for it. It was found in December 2023 (ABC7 and CNN coverage, https://abc7chicago.com/nasa-astronaut-tomato-in-space-frank-rubio/14155108/). Astronaut Scott Tingle wrote about a tool that stayed missing for weeks and was eventually found in a Soyuz (NASA blog, https://www.nasa.gov/blogs/spacestation/2018/05/31/captains-log-the-last-week).
- **[I]** The "lost" figures are roughly 3% across two decades. The search cost comes from the long tail plus the fixed overhead of manual IMS updates. Combining the two crew-time figures above with the $130k/h price gives a notional value of roughly $4-6M per year at full price. That is an upper-bound talking point, not a real budget line.

### 1.3 RFID: REALM-1, REALM-2 (Astrobee RFID Recon), REALM-3

- **[V] REALM-1** (deployed February 2017): 6 RFID readers and 24 fixed UHF antennas in 3 USOS modules, about 3,200 tagged items, and about 100 marker tags on structure. More than 8 billion tag reads were recorded. Accuracy in instrumented modules was **1.5 m mean error (σ 0.5 m, max 3 m)** and about **7.3 m in un-instrumented modules**. **All analysis runs on the ground and takes about 4.8 h.** REALM-1 helped search for about 100 missing items and gave some assistance on **60-70% of requests**. Failures correlated with cargo outside the instrumented zone, cargo buried before activation, or cargo unloaded while the system was down. REALM-1 moved to ISS sustaining operations at the end of FY19. (NTRS 20180007471; NASA spec sheet; Harvard LISH; NTRS 20210015701)
- **[V]** NASA's stated targets for the next generation: locate items **within 50 cm (basic) and 10 cm (specific)**, **request to in-hand within 30 s without ground involvement**, and REALM mass under 4.0 kg per module (currently about 6.6 kg). (NASA spec sheet)
- **[V]** NASA ran a Topcoder / NASA Tournament Lab challenge on ISS RFID localization: 5 weeks, $25,500 in prizes, 139 registrants, 25 submissions. The winner beat NASA's best benchmark by 28%, which roughly halves the search volume. Competitors produced x, y, z coordinates plus a **confidence radius**. (Harvard LISH)
- **[V] REALM-2 / "RFID Recon"** is a mobile RFID reader carried in Astrobee's payload bay (launched on NG-12 in October 2019). Science sessions 2021-2022 tested autonomous homing on a target tag and collected data on cargo moving from Columbus to the JEM. Some sessions were degraded by reader configuration problems in Node 2. (NASA ISS daily reports 2021-08-06, 2022-04-06, 2022-10-25; METECS, https://www.metecs.com/more-realm-2-astrobee-flights-on-iss; Astrobee IEEE TFR 2026, NTRS 20260001396)
- **[V] REALM-3** is a "dense zone" smart-enclosure concept: a shielded enclosure with an integrated reader. NASA's RFID taxonomy is **dense zone** (enclosures), **sparse zone** (open module volume), and **complex event processing** (inferring location from context). (NTRS 20180007471; NTRS 20160009157)
- **[V]** NASA notes that with reliable localization, CTB void space could fall from 25% to 5%, because bags could be packed for volume efficiency instead of for findability. (NTRS 20180007471)
- **[I]** The REALM figures give a useful baseline for a vision-based POC. "Room-level, 1.5 m, hours of latency" is what exists today. Vision can add *who* and *when* at the instant of a move, which RFID cannot. RFID can see inside bags, which vision cannot. The two are complementary.

### 1.4 Stowage practice: CTBs and locations

- **[V]** **Cargo Transfer Bags (CTBs)** are the standard soft stowage unit. External sizes: half, about 42.5 × 24.8 × 23.5 cm; **single (1.0), about 50.2 × 42.5 × 24.8 cm**; double, about 50.2 × 42.5 × 50.2 cm; triple, about 74.9 × 42.5 × 50.2 cm. Capacity ranges from about 30 lb (half) to 180 lb (triple). Larger **M-01/M-02/M-03** bags hold multiple CTBs. (STS-92 press kit, https://www.nasa.gov/wp-content/uploads/2023/05/sts-092-press-kit.pdf; NTRS 20160002631; GrabCAD/Stratasys packing data, secondary)
- **[V]** Stowage capacity is counted in **CTB Equivalents (CTBE)**: 1 CTBE = 0.053 m³ (1.86 ft³). Approved stowage capacity is about **2,300 CTBE (about 122 m³)** station-wide, with the PMM (Leonardo) the largest at 710 CTBE. (SSP 50621 Rev F, *Generic On-Orbit Stowage Capabilities and Requirements: Pressurized Volume*, DCN 001, 2016, https://imlive.s3.amazonaws.com/Federal%20Government/ID42272755263273611130549092035837226177/SSP_50621-RevF-DCN001-Collated_Master.pdf)
- **[V]** Stowage has overflowed into areas not designed for it, such as the joint airlock and the PMAs. Clearing the airlock before an EVA can take several hours. (AIAA 2006-5733) Stowage constraints include an 81 × 81 cm emergency translation corridor, plus clearances around fire extinguishers, vents, smoke detectors, and lights. (SSP 50621)
- **[V]** JPL built search and packing tools for ISS stowage officers, including fragment search (e.g., "ctb divider") and bag-packing algorithms. Containers often hold more than 30 items. (Knight et al., *Automating Stowage Operations for the ISS*, IWPSS 2013, https://ai.jpl.nasa.gov/public/documents/papers/knight-iwpss2013-automating.pdf)

### 1.5 Location codes (verified format)

**[V]** SSP 50621 lists USOS locations as `<MODULE><instance><FACE><BAY>`. Examples taken directly from the document:

- Node 1: `NOD1D1` (deck, on front), `NOD1O1`, `NOD1P1`, `NOD1S1`, through `NOD1D4` (RSR rack front), `NOD1P4` (galley rack)
- Node 2: `NOD2S2` (Stbd, toward Columbus), `NOD2P2` (Port, toward JPM), `NOD2D4` (DDCU ESA-1 / rack front), `NOD2O2`, and crew quarters `NOD2S5`/`D5`/`P5`/`O5` (CQ-1 to CQ-4)
- Node 3: uses **A/F** (aft/forward) faces as well, e.g. `NOD3F2` (PMM), `NOD3D2` (Cupola), `NOD3A2` (BEAM), `NOD3F4` (WHC toilet), `NOD3O2` (ARED)
- US Lab: `LAB1S1` to `LAB1S6`, `LAB1P1` to `LAB1P6`, `LAB1D1` to `LAB1D6`, `LAB1O1` to `LAB1O6`
- Columbus: `COL1A1` to `COL1A4`, `COL1F1` to `COL1F4`, `COL1D1` to `COL1D4`, `COL1O1` to `COL1O4`
- JEM PM: `JPM1A1` to `JPM1A6`, `JPM1F1` to `JPM1F6`, `JPM1D1` to `JPM1D6`, `JPM1O1` to `JPM1O5`; JEM ELM-PS: `JLP1A1`, `JLP1F1`, `JLP1P1`, `JLP1S1`, ...
- PMM: `PMM1S1` to `PMM1S4`, `PMM1P1` to `PMM1P4`, `PMM1D1` to `PMM1D4`, `PMM1O1` to `PMM1O4`, `PMM1C1` to `PMM1C4` (end cone)

Face letters: **S**tarboard, **P**ort, **D**eck, **O**verhead; in modules whose long axis runs port-starboard (Columbus, JPM, Node 3), **A**ft / **F**orward replace S/P. The document is a 2013/2016 revision, so some assignments have since changed (e.g., Nauka and Prichal are newer). **[I]** The face letters are not spelled out in a legend in the excerpt I read, but the pattern is consistent across every table. The Russian segment uses panel numbers and zones (e.g., FGB panels 101, 116, 201) rather than this scheme.

**[I]** Within a location there are sub-levels: rack, locker or tray, bag, sub-bag, item. The IMS models these as parent-child containers. The 2006 MIT paper explicitly flags "fluid parent-child relationships" as a pain point.

### 1.6 Station layout (module list)

**[V]** Current pressurized elements (NASA facts page, https://www.nasa.gov/international-space-station/space-station-facts-and-figures/; Wikipedia *Assembly of the ISS*). The station has 1,005 m³ of pressurized volume and **388 m³ habitable**. The typical crew is **7**.

- **USOS (U.S. Orbital Segment):**
  - Unity (Node 1)
  - Destiny (US Lab)
  - Quest joint airlock
  - Harmony (Node 2): the central hub, with Destiny aft, Kibo port, Columbus starboard
  - Columbus (ESA)
  - Kibo, JAXA (JEM PM + ELM-PS)
  - Tranquility (Node 3) with the Cupola
  - Leonardo PMM: moved to Node 3 forward in 2016, consistent with `NOD3F2` = PMM in SSP 50621
  - BEAM (expandable, used for stowage)
  - NanoRacks Bishop airlock
  - PMAs
- **Russian Orbital Segment:**
  - Zarya (FGB, now mostly storage; U.S.-funded and Russian-built)
  - Zvezda (Service Module)
  - Poisk (MRM-2)
  - Rassvet (MRM-1)
  - Nauka (MLM, 2021, at Zvezda nadir)
  - Prichal (2021)
  - Pirs has been deorbited.

**[V]** U.S. crew quarters are in Node 2 (`NOD2S5`/`D5`/`P5`/`O5` = CQ-1 to CQ-4). (SSP 50621)

---

## 2. Prior AI and vision work aboard the ISS

### 2.1 Astrobee

- **[V]** There are three free-flyers (Honey, Bumble, Queen), launched in 2019. Each is about a 30-32 cm cube of about 10 kg with fan propulsion and an autonomous dock. They had flown "hundreds of activities" for about two dozen projects by 2026.
  - **Computers:** LLP Wandboard; MLP Snapdragon 805, which runs navigation and vision; HLP Snapdragon 820, Android, for guest science.
  - **Cameras:** NavCam (1280×960 Bayer, 5 Hz for mapping), DockCam, **SciCam (autofocus color, live HD)**, HazCam and PerchCam (time-of-flight), SpeedCam.
  - (Astrobee IEEE TFR 2026, NTRS 20260001396; NTRS 20250005900; Dinkel et al., arXiv 2311.02558)
- **[V]** Communications: robots join the **ISS Payload LAN over station Wi-Fi**, or wired Ethernet when docked. Video goes to the ground as **H.264 over RTSP** on Ku-band. Commands and telemetry use NASA RAPID over **RTI DDS**. Because Wi-Fi and Ku-band drop out often, **robots log everything onboard for later download**. Storage is 30 GB on each robot plus 30 GB on the dock and the ISS NAS. **Loss of Signal (LOS) periods "vary from several seconds to tens of minutes."** (NTRS 20260001396)
- **[V]** Localization challenges: maps degrade quickly as the ISS gets rearranged. Changes in lighting break map matching; maps built in the dark transfer to bright conditions, but not the reverse. ORB-SLAM3 fails because of small volumes, planar scenes, and a monocular camera. (SpaceOps 2021, NTRS 20210014015; Kim, Coltin et al., ICRA 2017; arXiv 2411.01804)
  - Semantic-masking work used an object detector **fine-tuned on ISS data with eight classes**. Vents, lights, and handrails are treated as stable. **Cargo bags, wires, laptops, and racks are named as frequently moved.** (Semantic Masking and Visual Feature Matching for Robust Localization, arXiv 2411.01804)
- **[V]** Operations transferred to **Arkisys** in Summer 2025, with the first customer activity in November 2025. Astrobee is expected to run until the ISS is decommissioned. NASA explicitly lists "**help search for a missing item**" and replacing camcorder-based ISS **video surveys** as target uses. (NTRS 20260001396; USC ISI news, https://www.isi.edu/news/80324/)

### 2.2 ISAAC (Integrated System for Autonomous and Adaptive Caretaking)

- **[V]** ISAAC ran at NASA Ames and JSC from 2019, with 16 Astrobee ISS activities. Firsts:
  - first robot to autonomously navigate between multiple modules
  - first robot to localize a sound source inside a spacecraft (with Bosch SoundSee)
  - first robot survey of a spacecraft interior to build a map for human viewing (a 360° panorama stitched from 56 SciCam images)
  - (Smith et al., *Integration and Autonomy for Intra-Vehicular Robots: Results of the ISAAC Project*, ISSRDC 2025, NTRS 20250003945)
- **[V]** In the fault demo, a synthetic CO₂ anomaly made Astrobee inspect a mockup vent. **Computer vision detected the obstruction** (a printed image of a sock). The open-source detector is a **PyTorch CNN deployed with LibTorch** that classifies vents as free, obstacle, or unknown. It was trained on SciCam images captured in simulation. (https://nasa.github.io/isaac/html/ano.html; ISAAC repo https://github.com/nasa/isaac, Apache-2.0)
- **[V]** Other ISAAC components: a geometric streaming mapper (textured 3D mesh from HazCam plus SciCam), a volumetric mapper (e.g., Wi-Fi signal), a survey manager, and the Integrated Data Interface UI (https://github.com/nasa/isaac_user_interface). Gateway cargo transfer (Astrobee moving bags to a vehicle where a Robonaut stows them) was simulation only. (NTRS 20250003945)
- **[V]** AstrobeeCD (Dinkel et al., *Acta Astronautica* 223, 2024; arXiv 2311.02558) does 3D change detection on ISS survey data. Code: https://github.com/hollydinkel/astrobeecd. The paper notes that **SciCam images cannot be streamed due to size**, so the work defaults to NavCam.

### 2.3 CIMON / CIMON-2 (DLR, Airbus, IBM)

- **[V]** CIMON is a free-flying voice assistant in Columbus.
  - **CIMON-1:** 2018-2019, 14 months.
  - **CIMON-2:** launched December 2019 on CRS-19, planned for up to 3 years.
  - It has 2 cameras for **face recognition** and 5 for orientation and documentation.
  - It uses IBM Watson in the IBM cloud in Frankfurt, so processing happened **on the ground over the link**.
  - CIMON-2 added Watson Tone Analyzer emotion evaluation "if this is desired by the astronauts". The consent framing is explicit.
  - (DLR, https://www.dlr.de/en/latest/news/2020/02/20200415_cimon-2-makes-its-debut-on-the-iss; IBM newsroom)
  - I found no public end-of-mission results.

### 2.4 Int-Ball / Int-Ball2 (JAXA)

- **[V]** Int-Ball2 is a free-flying **camera robot** in Kibo. It has been in functional verification since June 2023. It navigates with visual SLAM plus IMU, docks and recharges autonomously, and is driven from the ground to film crew activities without crew setup. (IEEE RAM, September 2025, https://ramagazine.ieee.org/2025/09/10/int-ball2-on-orbit-demonstration-of-autonomous-intravehicular-flight-and-docking-for-image-capturing-and-recharging/; Epson 2024)

### 2.5 ISS camera systems

- **[V]** The ISS downlinks about **six simultaneous video channels** through most of each orbit, with dropouts during LOS. Missing segments can be downloaded later. (NTRS 20210014942, *Video from the ISS*; HDEV paper, NTRS 20170000416)
- **[V]** The internal camcorder baseline is moving to the Canon XF705. Video is recorded onboard in 4K and downlinked as files, while **routine live downlink stays HD**. (NASA 4K UHD Summit 2021, NTRS 20210023261)
- **[V]** The primary handheld stills and video camera is the **Nikon Z9** (13 bodies delivered in 2024). The D5 is still used for EVA. iPads are in use. (Nikon, https://www.nikon.com/company/news/2024/0207_mirrorless_03/)
- **[V]** **All live ISS video is monitored by the ISS Video Control Center, which cuts video if needed, and all recorded imagery gets an additional review before release.** (NTRS 20260001396)

### 2.6 Onboard AI precedent

- **[V]** **HPE Spaceborne Computer-2** (launched on NG-15, February 2021):
  - Two lockers, each holding a ProLiant DL360 Gen10 and an Edgeline EL4000 with a **low-power NVIDIA T4 GPU**.
  - Runs stock RHEL 7.8 on 28 V station power with no radiation hardening (it relies on redundancy).
  - Ran 24 experiments, including the Microsoft Azure **glove-damage image analyzer**. That analyzer ran inference onboard and sent only flagged results to the ground. It was "trial stage, not used for crucial safety decisions."
  - JPL benchmarked inference on Intel Myriad X and Snapdragon 855 attached to it.
  - (HPCwire 2021; The Register 2021-02-11; Microsoft blog 2022-04-04; Dunkel et al., ASTRA 2022, https://ai.jpl.nasa.gov/public/documents/papers/Dunkel-DL-ISS-ASTRA-2022.pdf)
- **[V]** **Axiom AxDCU-1** orbital data center prototype: a shoebox-sized node running **Red Hat Device Edge (MicroShift + RHEL + Ansible)**, launched to the ISS on SpX-33 (August 24, 2025) and sponsored by the ISS National Lab. The stated goal is to process data locally and send back only results. (ISS National Lab; The Register 2025-03-07)
  - **[I]** This is the closest precedent for running a Kubernetes-style containerized inference stack on the ISS.
- **[V]** NVIDIA "Space Computing" (GTC, March 16, 2026): Jetson Orin and IGX Thor are available now, and the Space-1 Vera Rubin module is future. Partners include Axiom and Planet. **The release does not mention the ISS or DGX Spark.** I found **no public record of a Jetson or DGX Spark operating inside the ISS.** (NVIDIA press release)

### 2.7 Future platforms

- **[V] Gateway:** plans called for it to be uncrewed more than 85% of the time, and an IVR working group studied robotic caretaking and cargo transfer. (NTRS 20260001396) **However, NASA paused Gateway "in its current form" on March 24, 2026**, pivoting toward a lunar surface base. Its final disposition is unsettled. (Wikipedia *Lunar Gateway*, secondary)
- **[V] ISS end of life:** SpaceX U.S. Deorbit Vehicle, with retirement around 2030 and reentry about 2030-2031. The FY2026 budget pressured crew size; Dana Weigel discussed going from 4 to 3 USOS crew. (Spaceflight Now 2024-07-18; Aerospace America)
- **[V] Commercial LEO destinations:**
  - Vast Haven-1: 2027 per February 2026 reporting, crews of 4 for about 10 days.
  - Axiom: first module attaching to the ISS around 2026-2027.
  - Starlab: target 2029.
  - Orbital Reef: late 2020s to 2030.
  - In March 2026 NASA floated a government-owned core module approach. Phase 2 CLD awards were not confirmed as of my search.
  - (NASASpaceflight 2026-02; MIT Technology Review 2026-01-12)
  - **[I]** Commercial stations will be smaller, have smaller crews and ground teams, and generate more crew-tended and uncrewed hours. All of that raises the value of automated "where is X" capability, and these stations are a more plausible near-term customer than the ISS itself.

---

## 3. Object classes and environment challenges

### 3.1 Typical object classes

Verified examples come from the sources above. The grouping is mine.

- **Containers:** CTBs (half, single, double, triple), M-bags, Bulk Overwrap Bags (food, one per menu category), crew preference containers, lost-and-found bags, trash bags, and Ziplocs. [V: SSP 50621; NTRS 20200001311; AIAA 2006-5733]
- **Food:** rehydratable packs (clear multi-layer film with a septum), thermostabilized retort pouches (foil laminate), and beverage pouches. [V: NTRS 20100026032 and related]
- **Electronics:** station laptops, iPads, **Nikon Z9 cameras and lenses**, the Canon XF705 camcorder, cables and power cords, and headsets. [V: Nikon; NTRS 20210023261; MSFC stowage article]
- **Tools:** IVA tools such as Allen wrenches, drivers, and duct tape (Sandy Magnus's lost Allen wrench, 2008). [V: Air & Space, secondary]
- **Hygiene and crew items:** wipes, clothing, socks (the ISAAC test "anomaly" was a sock), and towels. [V: AIAA 2006-5733; ISAAC]
- **Science hardware:** payload boxes, glove boxes, MELFI freezer contents, and plant habitats (the tomato). [V: SSP 50621]
- **Robots:** Astrobee, Int-Ball2, and CIMON. [V]
- **Fixed structure (useful as landmarks):** handrails, vents, lights, racks, hatches, and labels. [V: arXiv 2411.01804]

### 3.2 Visual and audio challenges

- **[V] Lighting:** the Solid State Lighting Assemblies have three modes: **4500 K general, 2700 K pre-sleep, and 6500 K phase-shift**. Lights are dimmed at night, and illumination changes break visual localization. (NTRS 20160005080 and related; Kim et al., ICRA 2017)
- **[V] Constant rearrangement:** cargo bags, wires, laptops, and racks move "between flights". Maps age quickly. (arXiv 2411.01804; NTRS 20210014015)
- **[V] Floating items:** untethered items drift in three dimensions (AIAA 2006-5733). Items are held with Velcro (the tomato was "Velcroed where it was supposed to be"). Clutter spills into non-stowage areas.
- **[I]** Further challenges I expect:
  - No canonical "up", so detectors trained on Earth imagery with a gravity prior will have trouble.
  - Heavy occlusion by bags, bungees, and cables.
  - Many visually near-identical white Nomex CTBs, so identity has to come from labels or barcodes and context, not appearance.
  - Reflective metal and plastic surfaces and window glare (Cupola).
  - Small objects (tools, food packs) at fixed-camera distances.
  - Crew motion blur.
  - Fisheye or wide-angle distortion on survey cameras.
- **[V] Audio:** U.S. requirements are NC-50 awake and NC-40 asleep. Historical module levels are about 56-69 dBA. Flight-rule noise limits were exceeded in 41% of dosimetry measurements since 2008, and Nauka has significant exceedances. (Inter-Noise 2021, NTRS 20210016305; ICES 2024, NTRS 20240006442)
  - **[I]** Speech-based event capture (e.g., "I'm putting the drill in the CTB at NOD2D4") must be robust to continuous fan noise of around 60 dBA. Close-talk microphones or headsets will beat room microphones.

---

## 4. Data and compute constraints

- **[V] Ku-band via TDRS:**
  - Return link went from 300 to **600 Msps** (operational May 2019), which is about **517 Mbps of user data**.
  - The forward (uplink) link stays at about **21 Mbps (25 Msps)**.
  - Ku coverage is about 85% of the orbit.
  - The upgrade also added a 1 Gbps interface to the Joint Station LAN.
  - (NASA POIWG briefing, July 26, 2016, NTRS 20160009326; NASA Space Network project summary, NTRS 20190025199)
- **[V] Latency and dropouts:** the Ku-band downlink runs up to 500 Mbps with **about 600 ms round-trip time**. NASA has demonstrated High-rate DTN onboard, including store-and-forward and LOS recovery (Dudukovich et al., *Advances in High-rate Delay Tolerant Networking On-board the ISS*, IEEE SMC-IT/SCC 2024, NTRS 20240007078). LOS periods last from seconds to tens of minutes (NTRS 20260001396).
- **[V] Optical:** ILLUMA-T demonstrated **1.2 Gbps** return and about 155 Mbps forward via LCRD (2023-2024), then was removed. (NASA ILLUMA-T page; NTRS 20250000325)
- **[V]** The Ku-band link is **shared** with many users and payloads, which compete for it. (*European Journal of Remote Sensing* technical note, 2017)
- **[V] Onboard compute precedent:**
  - SB-2 (T4 GPU, x86, RHEL)
  - AxDCU-1 (Red Hat Device Edge)
  - Astrobee Snapdragons
  - Payload LAN, ISS NAS, and Wi-Fi
  - (sources above)
- **[V] Reference edge boxes (terrestrial specs):**
  - **DGX Spark** (GB10): 128 GB unified LPDDR5x at 273 GB/s, "1 PFLOP FP4 (sparse)", 150×150×50.5 mm, 1.2 kg. (NVIDIA docs, https://docs.nvidia.com/dgx/dgx-spark/hardware.html)
  - **Jetson AGX Thor:** 128 GB, 2070 FP4 TFLOPS sparse, **40-130 W** configurable.
- **[I]** None of these units are flight-certified for the ISS. A real deployment would go through payload safety (offgassing, touch temperature, EMI, power on 28 V or 120 V DC), which takes months to years. For a POC, a DGX Spark or Jetson serves as a **ground surrogate for the onboard edge node**, and the realistic flight analogs are SB-2-class or AxDCU-class hardware.
- **[I] Bandwidth math:**
  - Six HD H.264 streams at about 5-8 Mbps each total roughly 30-50 Mbps, which is within the 517 Mbps return link.
  - Raw multi-camera 4K plus audio from many fixed cameras would compete with science data.
  - The 21 Mbps uplink limits model updates. A 7B-parameter vision-language model at 4-bit is about 4 GB, so roughly 30 min at full forward rate; in practice longer, since the link is shared.
  - **Event metadata is tiny:** a JSON sighting with a 50 KB thumbnail at 10 events per minute is under 0.1 Mbps.

### 4.1 Edge vs. ground options

| Option | Pros | Cons |
|---|---|---|
| A. Ground-only: run models on existing downlinked video | No flight hardware; works today with archived or sim video; matches how MSFC already spots items on live video | Bound by which feeds are downlinked and when; LOS gaps; about 600 ms+ latency; Video Control Center gating |
| B. Edge-first: onboard inference, downlink events and thumbnails | Tiny bandwidth; works through LOS (store-and-forward via DTN); privacy filtering before downlink; matches SB-2 glove and AxDCU "send results only" precedent | Needs flight-qualified compute; harder model updates over a 21 Mbps uplink |
| C. Hybrid: edge detection and tracking, ground re-identification and aggregation, raw clips on demand | Best fit; ground keeps the authoritative inventory and audit log | More moving parts |

---

## 5. Privacy, crew consent, and AI policy

- **[V]** Medical data is protected by regulation. 14 CFR §1214.403 (ISS Crew Code of Conduct) treats all personal medical information, biomedical telemetry, and private medical communications as private. NASA-STD-3001 requires routinely scheduled private medical communications. (https://www.law.cornell.edu/cfr/text/14/1214.403; NASA OCHMO technical briefs)
- **[V]** Research use of crew data requires **informed consent under the NASA Flight IRB**. Re-use of potentially identifiable data requires consent. (NASA LSAH newsletter, October 2024)
- **[V]** Privacy is a recognized psychosocial factor (team cohesion, privacy, isolation) monitored in crew behavioral health. (NASA OCHMO health and medical care reference; PMC12612093)
- **[V]** Operational precedent from Astrobee:
  - Flying cameras are called "a novel challenge for astronaut privacy and respecting payload proprietary restrictions".
  - **All live video is monitored by the ISS Video Control Center, which cuts it if needed.**
  - All recorded imagery is reviewed before release.
  - **Blue lights on all four sides turn on whenever the microphone records.**
  - Astrobee activities are announced at the daily planning conference, and robots announce their presence visually and audibly.
  - (NTRS 20260001396)
- **[V]** CIMON-2's emotion analysis ran only "if desired by the astronauts". (DLR)
- **[V] NASA AI principles:** *NASA Framework for the Ethical Use of AI* (NASA/TM-20210012886, April 2021) has six principles:
  - fair
  - explainable and transparent
  - accountable
  - secure and safe
  - human-centric and societally beneficial (including "inform humans when AI is used")
  - scientifically and technically robust
  - NASA OIG (IG-23-012) found NASA lacked a unified AI definition.
- **[V]** NASA publishes an annual **AI use case inventory**. The 2024 public highlights page (January 2025) lists 17 use cases, none for ISS inventory or crew video (https://www.nasa.gov/organizations/ocio/dt/ai/2024-ai-use-cases/). The current federal framework is **OMB M-25-21** (April 2025), with agency compliance plans due September 30, 2025 and a "high-impact AI" review. NASA's first Chief AI Officer was named in May 2024 (David Salvagnini); a later page lists Kevin Murphy as acting CDO/CAIO.
- **[V] NASA media guidelines:** imagery is generally not copyrighted, but current astronauts' "names, likenesses or other personality traits" may not appear on commercial products or advertising. **NASA does not permit its insignia to be used in training AI tools or to appear with AI-generated imagery.** (https://www.nasa.gov/nasa-brand-center/images-and-media/)
- **[I] Policy implications:**
  - A system that identifies people and records "who moved it" is likely to be classed as rights- or safety-relevant AI under M-25-21-style review, because it processes biometric or identity data about federal employees.
  - Expect requirements for:
    - opt-in consent (likely through IRB-like or flight rule processes)
    - no-capture zones: crew quarters `NOD2S5`/`D5`/`P5`/`O5`, the WHC/toilet `NOD3F4`, medical areas, and private medical or family conferences
    - visible recording indicators
    - data minimization (store tracks and embeddings, not raw faces)
    - retention limits
    - human-in-the-loop for any consequential use
  - International partner crew (Roscosmos, ESA, JAXA, CSA) add jurisdictional complexity.
  - Attribution should start with crew-badge or role-level identity ("CDR", "FE-2") rather than face recognition, or be optional.

---

## 6. Public datasets and imagery for a POC

| Asset | What it is | License / terms | Link |
|---|---|---|---|
| **Astrobee ISS Free-Flyer Datasets** (Kang et al., IEEE RA-L 9(4), 2024) | 25 sequences, about 103 min; NavCam gray images at 5 Hz + IMU at 100 Hz; JEM, US Lab, Node 2; ground-truth 6-DoF poses; CAD models; ROS bags | **No license stated** on the page (ask the authors) | https://astrobee-iss-dataset.github.io/ |
| Astrobee flight software | ROS stack, simulator (Gazebo), ISS world | Apache-2.0 | https://github.com/nasa/astrobee |
| astrobee_media | ISS interior meshes (from IGOAL), handrails, dock, granite lab | "NASA Media Usage Guidelines" | https://github.com/nasa/astrobee_media |
| ISAAC | Mapping, anomaly CNN (vent), survey manager, UI; ISS activity data partly open | Apache-2.0 | https://github.com/nasa/isaac |
| AstrobeeCD | Change detection on ISS survey data | per repo | https://github.com/hollydinkel/astrobeecd |
| NASA Image and Video Library API | Searchable imagery. Hit counts on 2026-10-17: "Kibo laboratory" 2,528; "stowage" 1,805; "Harmony module" 1,426; "space station interior" 832; "cargo transfer bag" 21 | Generally public domain; NASA guidelines apply; identifiable astronauts are restricted for commercial or AI-branding use | https://images-api.nasa.gov/search |
| Expedition galleries | iss0NNe… frames of interior crew activity | same | nasa.gov expedition galleries |
| NASA 3D Resources | ISS exterior glTF and Lightwave models (exterior; 2011-era hi-res) | U.S. Government work; free to use | https://science.nasa.gov/3d-resources/ |
| Roboflow "ISS object detection" | Community datasets (wire, obstacles, ...) | Varies; unvetted | universe.roboflow.com |

- **[V]** I found **no official NVIDIA Isaac Sim / USD ISS interior asset**, and no public labeled ISS object-detection dataset from NASA. The eight-class ISS detector in arXiv 2411.01804 does not appear to have released its labels.
- **[I] Practical path for synthetic data:**
  1. Import the astrobee_media ISS interior meshes into Isaac Sim (Gazebo meshes convert to USD).
  2. Populate them with CTB, laptop, food pack, and tool models.
  3. Randomize lighting across 2700-6500 K, object orientation (no gravity prior), clutter, and occlusion.
  4. Validate on real NASA library imagery.

  Do not use the NASA insignia or astronaut likenesses in training or demo material without clearance.

---

## Implications for the POC

### A. Object catalog (v0)

| Tier | Classes | Identity strategy |
|---|---|---|
| 1. Containers | CTB (half, 1, 2, 3), M-bag, BOB (food overwrap), Ziploc, lost-and-found bag, trash bag | Class + OCR/barcode of IMS label (Code 39) when visible + track continuity |
| 2. High-value loose items | Laptop, iPad, Nikon Z9 camera and lens, camcorder, headset, cable/power cord, drill/driver, wrench, duct tape roll, flashlight | Class + appearance embedding + optional barcode |
| 3. Consumables | Rehydratable pack, retort pouch, beverage pouch, wipes, towel, clothing item (sock, shirt) | Class only (bag-level, matching IMS practice) |
| 4. Science / robots | Payload box, sample bag, plant habitat, Astrobee, Int-Ball2 | Class + location |
| 5. Static landmarks (not inventory) | Handrail, vent, light panel, rack face, hatch, location barcode label | Used for localization and anchoring; ISS-proven per arXiv 2411.01804 |
| People | Crew member (role or badge ID, opt-in face ID) | See privacy section |

**Rule:** treat "near-identical CTBs" as unresolvable by appearance alone. Confidence should drop unless a label is read or the track is continuous.

### B. Location model

- Use a hierarchical key that mirrors IMS and SSP 50621: `segment > module > face+bay (e.g., NOD2D4) > container (barcode) > sub-container > item`. Add an optional continuous coordinate (x, y, z in a module frame) with a **confidence radius**, the same output format as the NASA RFID challenge.
- Seed the location table from the SSP 50621 code lists (Node 1-3, LAB1, COL1, JPM1, JLP1, PMM1). Add Russian segment modules at module level only.
- Map each camera's field of view to a set of face/bay codes, so a detection in a pixel region resolves to the nearest location code.
- Flag no-capture zones in the schema: crew quarters, WHC, and medical areas.

### C. Dashboard fields ("items last seen")

`item_id`, `class`, `display_name`, `ims_barcode` (nullable), `parent_container_id`, `last_seen_ts (GMT, the ISS time standard)`, `location_code`, `module`, `xyz_m` + `radius_m`, `confidence` (0-1, split into detection, identity, and location components), `moved_by` (role or crew ID, nullable, consent-gated), `event_type` (seen, moved, stowed, unstowed, consumed, discarded, lost-candidate), `source_camera`, `snapshot_uri` (privacy-redacted thumbnail), `audio_cue` (transcript snippet if a spoken stow call was matched), `ims_location` (from IMS) and a `discrepancy` flag when vision and IMS disagree, `history[]` (last N locations, since the "check prior locations" workflow is how lost items are actually found).

Useful views:
- a "lost-candidate" queue (IMS says X, vision hasn't seen it in N days)
- an LOS backlog indicator
- per-module heat map
- reconciliation export to the ISO console workflow, as a suggested IMS move rather than a direct write

### D. Data-link design

- **Edge (onboard surrogate = DGX Spark or Jetson Thor in the lab):**
  - Decode camera streams.
  - Run detection and tracking.
  - Read labels with OCR.
  - Do speech-to-text on the audio.
  - Apply the privacy filter (blur faces and people in no-capture zones, drop audio outside stow calls).
  - Emit **events + thumbnails**.
- **Transport:** event bus over a DTN-style store-and-forward path to tolerate LOS of seconds to tens of minutes. Sequence numbers allow gap-free replay. Bandwidth budget is under 1 Mbps for events, with optional low-rate H.264 clips on demand. Never assume uplink above about 21 Mbps, and plan model updates as scheduled file transfers.
- **Ground:** authoritative inventory DB, cross-camera re-identification, IMS reconciliation, dashboard, and audit log of every AI assertion (who, what, when, model version) for NASA's explainability and accountability principles.
- **Lab emulation:** inject roughly 600 ms RTT, scheduled LOS windows, and a 500/21 Mbps asymmetric link with `tc netem` to show that the design survives realistic ISS conditions.

---

## Open questions

1. **Camera access:** Which internal feeds (fixed cameras, camcorder, Astrobee SciCam, Int-Ball2) could feed such a system, at what resolution and frame rate, and do Video Control Center review rules allow automated processing of live feeds?
2. **IMS integration:** Is there any read API or export of IMS (current schema, location code tables beyond SSP 50621 Rev F) available to a POC team? Is write-back ever acceptable, or only "suggested moves"?
3. **Identity and consent:** Would NASA, ESA, JAXA, and Roscosmos crews accept face-based attribution? Or should "moved by" use role badges, wearable tags, or voice self-report? What review path applies (Flight IRB vs. operational flight rule vs. M-25-21 high-impact determination)?
4. **Current RFID status:** Is REALM-1 still operating post-FY19 sustaining, and could RFID reads be fused with vision? (I found no 2024-2026 REALM results.)
5. **Russian segment:** Russian segment coverage and location codes (panel-based) are separate. Is the ROS in scope?
6. **Compute path to flight:** Would the hardware target be SB-2 (T4, ageing), AxDCU-class, or a new Jetson Thor / IGX Thor payload? What power, thermal, and certification budget is realistic before about 2030 retirement?
7. **Target platform:** Given ISS retirement around 2030 and Gateway paused, is the real customer a commercial LEO station (Axiom, Vast, Starlab) or a lunar surface habitat? Their camera and IT architectures are not yet public.
8. **Ground-truth data:** Will NASA share labeled interior imagery, or Astrobee survey SciCam imagery, under a usable license? (The Astrobee ISS dataset page states no license.)
9. **Audio:** Is crew audio capture permissible at all outside existing comm loops? Would a "spoken stow call" convention (push-to-talk) be acceptable to crews?
10. **Success metric:** Should the POC adopt NASA's own targets (50 cm basic and 10 cm specific localization, request to in-hand in 30 s without ground) as its evaluation bar?
