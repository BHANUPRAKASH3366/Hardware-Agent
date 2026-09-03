"""Component category tree.

Two jobs:

1. **Query understanding.** When someone searches "transimpedance amplifiers"
   rather than a part number, `match()` resolves it to a category node so the
   agent can send every distributor a clean, canonical keyword instead of the
   raw phrase, and tell the user how the query was interpreted.

2. **Reference parts.** Each leaf carries representative real part numbers that
   feed the offline catalogue, so a category search returns something concrete
   before any distributor API key is configured.

The structure follows the standard semiconductor category taxonomy. Adding a
branch here automatically extends search, the category browser and the offline
catalogue -- nothing else needs editing.

Part tuples are (mpn, manufacturer, description, package, indicative USD price).
Prices are indicative only; live pricing always comes from the distributors.
"""
import copy
import re

CURATED_TREE = [
    {
        "id": "amplifiers", "name": "Amplifiers",
        "aliases": ["amplifier", "amp", "amps"],
        "children": [
            {
                "id": "comparators", "name": "Comparators",
                "aliases": ["comparator", "voltage comparator"],
                "parts": [
                    ("LM393P", "Texas Instruments", "Dual differential comparator, 2 to 36 V", "PDIP-8", 0.35),
                    ("LM339N", "Texas Instruments", "Quad differential comparator, open collector", "PDIP-14", 0.42),
                    ("TLV3501AIDBVR", "Texas Instruments", "4.5 ns rail-to-rail high-speed comparator", "SOT-23-5", 2.10),
                ],
            },
            {
                "id": "current-sense-amplifiers", "name": "Current-sense amplifiers",
                "aliases": ["current sense amplifier", "current sense amp", "csa", "current shunt monitor"],
                "children": [
                    {
                        "id": "analog-current-sense-amplifiers",
                        "name": "Analog current-sense amplifiers",
                        "aliases": ["analog current sense amplifier"],
                        "parts": [
                            ("INA180A1IDBVR", "Texas Instruments", "Bidirectional current-sense amplifier, 20 V/V", "SOT-23-5", 0.55),
                            ("INA240A2PWR", "Texas Instruments", "80 V bidirectional CSA with PWM rejection, 50 V/V", "TSSOP-8", 2.30),
                            ("INA181A1IDBVR", "Texas Instruments", "Low-power bidirectional current-sense amplifier", "SOT-23-5", 0.62),
                        ],
                    },
                    {
                        "id": "analog-csa-integrated-shunt",
                        "name": "Analog current-sense amplifiers with integrated shunt resistor",
                        "aliases": ["current sense amplifier with shunt", "integrated shunt amplifier"],
                        "parts": [
                            ("INA250A2PW", "Texas Instruments", "Current-sense amplifier with 2 mOhm integrated shunt", "TSSOP-16", 5.85),
                            ("INA253A2PWR", "Texas Instruments", "Precision CSA with integrated shunt, PWM rejection", "TSSOP-16", 6.40),
                        ],
                    },
                    {
                        "id": "digital-power-monitors", "name": "Digital power monitors",
                        "aliases": ["digital power monitor", "power monitor", "i2c power monitor"],
                        "parts": [
                            ("INA219AIDR", "Texas Instruments", "High-side current/power monitor with I2C", "SOIC-8", 2.15),
                            ("INA226AIDGSR", "Texas Instruments", "Bidirectional current/power monitor, 16-bit, I2C", "VSSOP-10", 2.05),
                            ("INA228AIDGSR", "Texas Instruments", "85 V, 20-bit precision power monitor, I2C", "VSSOP-10", 3.10),
                            ("INA3221AIRGVR", "Texas Instruments", "Three-channel current and bus voltage monitor", "VQFN-16", 2.45),
                        ],
                    },
                    {
                        "id": "digital-power-monitors-integrated-shunt",
                        "name": "Digital power monitors with integrated shunt resistor",
                        "aliases": ["power monitor with shunt"],
                        "parts": [
                            ("INA260AIPWR", "Texas Instruments", "Digital power monitor with 2 mOhm integrated shunt, I2C", "TSSOP-16", 5.20),
                        ],
                    },
                ],
            },
            {
                "id": "difference-amplifiers", "name": "Difference amplifiers",
                "aliases": ["difference amplifier", "differential amplifier"],
                "parts": [
                    ("INA149AIDR", "Texas Instruments", "High common-mode voltage difference amplifier, +/-275 V", "SOIC-8", 6.20),
                    ("INA117P", "Texas Instruments", "High common-mode difference amplifier, +/-200 V", "PDIP-8", 8.90),
                    ("INA132UA", "Texas Instruments", "Low-power, single-supply difference amplifier", "SOIC-8", 4.10),
                ],
            },
            {
                "id": "fully-differential-amplifiers", "name": "Fully differential amplifiers",
                "aliases": ["fully differential amplifier", "fda", "adc driver"],
                "parts": [
                    ("THS4551IDGKR", "Texas Instruments", "Low-noise precision fully differential amplifier, 150 MHz", "VSSOP-8", 3.40),
                    ("THS4521IDGKR", "Texas Instruments", "Very low power rail-to-rail FDA, 145 MHz", "VSSOP-8", 3.10),
                    ("LMH6552MA", "Texas Instruments", "1.5 GHz fully differential amplifier", "SOIC-8", 5.60),
                ],
            },
            {
                "id": "instrumentation-amplifiers", "name": "Instrumentation amplifiers",
                "aliases": ["instrumentation amplifier", "in-amp", "inamp"],
                "parts": [
                    ("INA128P", "Texas Instruments", "Precision, low-power instrumentation amplifier", "PDIP-8", 8.50),
                    ("INA333AIDGKR", "Texas Instruments", "Micro-power, zero-drift instrumentation amplifier", "VSSOP-8", 4.20),
                    ("AD620ANZ", "Analog Devices", "Low-cost, low-power instrumentation amplifier", "PDIP-8", 8.80),
                    ("AD8232ACPZ-R7", "Analog Devices", "Single-lead heart-rate analog front end", "LFCSP-20", 6.30),
                ],
            },
            {
                "id": "op-amps", "name": "Operational amplifiers (op amps)",
                "aliases": ["op amp", "op amps", "opamp", "opamps", "operational amplifier"],
                "children": [
                    {
                        "id": "audio-op-amps", "name": "Audio op amps",
                        "aliases": ["audio op amp", "audio operational amplifier"],
                        "parts": [
                            ("OPA1612AIDR", "Texas Instruments", "SoundPlus high-performance dual audio op amp", "SOIC-8", 4.60),
                            ("NE5532P", "Texas Instruments", "Dual low-noise audio operational amplifier", "PDIP-8", 0.75),
                            ("OPA2134PA", "Texas Instruments", "Dual FET-input audio op amp, ultra-low distortion", "PDIP-8", 4.15),
                        ],
                    },
                    {
                        "id": "general-purpose-op-amps", "name": "General-purpose op amps",
                        "aliases": ["general purpose op amp", "jellybean op amp"],
                        "parts": [
                            ("LM358P", "Texas Instruments", "Dual general-purpose operational amplifier", "PDIP-8", 0.42),
                            ("LM324N", "Texas Instruments", "Quad general-purpose operational amplifier", "PDIP-14", 0.38),
                            ("TL072CP", "Texas Instruments", "Dual low-noise JFET-input operational amplifier", "PDIP-8", 0.55),
                            ("MCP6002-I/P", "Microchip", "Dual rail-to-rail 1 MHz op amp, 1.8 V", "PDIP-8", 0.48),
                        ],
                    },
                    {
                        "id": "high-speed-op-amps", "name": "High-speed op amps (GBW >= 50 MHz)",
                        "aliases": ["high speed op amp", "wideband op amp"],
                        "parts": [
                            ("OPA695IDBVR", "Texas Instruments", "Ultra-wideband current-feedback op amp, 1.4 GHz", "SOT-23-6", 4.10),
                            ("THS3491IDDAR", "Texas Instruments", "900 MHz, 500 mA high-power output amplifier", "SO-8 PowerPAD", 8.30),
                            ("LMH6702MA", "Texas Instruments", "Ultra-low distortion wideband op amp, 720 MHz", "SOIC-8", 5.10),
                        ],
                    },
                    {
                        "id": "power-op-amps", "name": "Power op amps",
                        "aliases": ["power op amp", "high current op amp"],
                        "parts": [
                            ("OPA548T", "Texas Instruments", "High-voltage, high-current power operational amplifier", "TO-220-7", 12.50),
                            ("OPA541AP", "Texas Instruments", "High-power monolithic op amp, 5 A", "TO-3 / PDIP", 24.90),
                            ("LM675T", "Texas Instruments", "Power operational amplifier, 3 A", "TO-220-5", 3.75),
                        ],
                    },
                    {
                        "id": "precision-op-amps", "name": "Precision op amps (Vos < 1 mV)",
                        "aliases": ["precision op amp", "low offset op amp", "zero drift op amp"],
                        "parts": [
                            ("OPA277PA", "Texas Instruments", "High-precision operational amplifier, 10 uV offset", "PDIP-8", 5.40),
                            ("OPA189IDBVR", "Texas Instruments", "Zero-drift precision op amp, 0.4 uV offset", "SOT-23-5", 3.95),
                            ("OPA2188AIDR", "Texas Instruments", "Dual zero-drift precision op amp", "SOIC-8", 4.55),
                        ],
                    },
                ],
            },
            {
                "id": "pga-vga", "name": "Programmable & variable gain amplifiers (PGAs & VGAs)",
                "aliases": ["pga", "vga", "programmable gain amplifier", "variable gain amplifier"],
                "parts": [
                    ("PGA113AIDGSR", "Texas Instruments", "Programmable gain amplifier with SPI, binary gains", "VSSOP-10", 4.80),
                    ("PGA281AIPW", "Texas Instruments", "High-precision programmable gain instrumentation amplifier", "TSSOP-16", 9.60),
                    ("VCA821IRGVT", "Texas Instruments", "Wideband variable gain amplifier, 710 MHz", "VQFN-16", 7.40),
                ],
            },
            {
                "id": "rf-amplifiers", "name": "RF amplifiers",
                "aliases": ["rf amplifier", "rf amp"],
                "children": [
                    {
                        "id": "rf-fda", "name": "RF fully differential amplifiers (FDAs)",
                        "aliases": ["rf fully differential amplifier", "rf fda"],
                        "parts": [
                            ("LMH5401IRMST", "Texas Instruments", "8 GHz ultra-wideband fully differential amplifier", "UQFN-16", 12.80),
                            ("LMH3401IRMZT", "Texas Instruments", "7 GHz fixed-gain fully differential amplifier", "UQFN-16", 14.20),
                        ],
                    },
                    {
                        "id": "rf-gain-blocks", "name": "RF gain block amplifiers",
                        "aliases": ["rf gain block", "gain block amplifier"],
                        "parts": [
                            ("TQP3M9037", "Qorvo", "Wideband RF gain block, 0.05 to 4 GHz, 13 dB", "QFN-8", 2.20),
                            ("PGA-103+", "Mini-Circuits", "Low-noise RF gain block, DC to 4 GHz", "SOT-89", 4.65),
                        ],
                    },
                    {
                        "id": "rf-lna", "name": "RF low noise amplifiers (LNAs)",
                        "aliases": ["lna", "low noise amplifier", "rf lna"],
                        "parts": [
                            ("TQP3M9028", "Qorvo", "Ultra-low-noise RF amplifier, 0.6 to 2.7 GHz, 0.5 dB NF", "QFN-8", 1.95),
                            ("SKY67151-396LF", "Skyworks", "Low-noise amplifier, 0.7 to 3.8 GHz", "QFN-8", 2.40),
                            ("BGA2803", "Nexperia", "MMIC wideband amplifier, 0.05 to 4 GHz", "SOT-343", 1.05),
                        ],
                    },
                    {
                        "id": "rf-vga", "name": "RF variable gain amplifiers (VGAs)",
                        "aliases": ["rf vga", "rf variable gain amplifier"],
                        "parts": [
                            ("HMC625BLP5E", "Analog Devices", "6-bit digital variable gain amplifier, DC to 1.3 GHz", "QFN-32", 34.50),
                            ("VCA824IDGSR", "Texas Instruments", "Ultra-wideband linear-in-V variable gain amplifier", "VSSOP-10", 8.10),
                        ],
                    },
                ],
            },
            {
                "id": "special-function-amplifiers", "name": "Special function amplifiers",
                "aliases": ["special function amplifier"],
                "children": [
                    {
                        "id": "signal-conditioners-4-20ma", "name": "4-20mA signal conditioners",
                        "aliases": ["4-20ma", "4 to 20 ma transmitter", "current loop transmitter"],
                        "parts": [
                            ("XTR116U", "Texas Instruments", "4-20 mA current loop transmitter with 5 V reference", "SOIC-8", 6.40),
                            ("XTR111AIDGQR", "Texas Instruments", "Precision voltage-to-current converter, 0-20 mA", "VSSOP-10", 5.20),
                            ("RCV420JP", "Texas Instruments", "Precision 4-20 mA current loop receiver", "PDIP-16", 22.80),
                        ],
                    },
                    {
                        "id": "frequency-converters", "name": "Frequency converters",
                        "aliases": ["frequency converter", "mixer", "downconverter", "upconverter"],
                        "parts": [
                            ("TRF371135IRGZR", "Texas Instruments", "Wideband IQ demodulator / downconverter", "VQFN-48", 18.00),
                            ("ADL5801ACPZ-R7", "Analog Devices", "10 MHz to 6 GHz high-linearity active mixer", "LFCSP-20", 11.40),
                        ],
                    },
                    {
                        "id": "isolated-amplifiers", "name": "Isolated amplifiers",
                        "aliases": ["isolated amplifier", "isolation amplifier"],
                        "parts": [
                            ("AMC1200SDUBR", "Texas Instruments", "Fully differential isolated amplifier for shunt sensing", "SOP-8", 3.90),
                            ("AMC1301DWVR", "Texas Instruments", "Precision reinforced isolated amplifier", "SOIC-8 wide", 4.30),
                            ("ISO124U", "Texas Instruments", "Precision lowest-cost isolation amplifier", "SOIC-16 wide", 21.60),
                        ],
                    },
                    {
                        "id": "line-drivers", "name": "Line drivers",
                        "aliases": ["line driver", "differential line driver"],
                        "parts": [
                            ("SN65LVDS1DBVR", "Texas Instruments", "High-speed differential LVDS line driver", "SOT-23-5", 1.85),
                            ("DS90C401M", "Texas Instruments", "Dual LVDS line driver, 155 Mbps", "SOIC-8", 3.20),
                            ("THS6012IDWP", "Texas Instruments", "Dual differential ADSL line driver, 500 mA", "SO PowerPAD-20", 9.85),
                        ],
                    },
                    {
                        "id": "logarithmic-amplifiers", "name": "Logarithmic amplifiers",
                        "aliases": ["log amp", "logarithmic amplifier"],
                        "parts": [
                            ("LOG112AID", "Texas Instruments", "Precision logarithmic and log ratio amplifier", "SOIC-16", 14.50),
                            ("LOG114AIRGVT", "Texas Instruments", "Single-supply high-speed precision log amplifier", "VQFN-16", 12.30),
                            ("AD8307ARZ", "Analog Devices", "Low-cost DC to 500 MHz, 92 dB logarithmic amplifier", "SOIC-8", 9.20),
                        ],
                    },
                    {
                        "id": "sample-and-hold-amplifiers", "name": "Sample & hold amplifiers",
                        "aliases": ["sample and hold", "sample & hold amplifier", "track and hold"],
                        "parts": [
                            ("LF398N", "Texas Instruments", "Monolithic sample-and-hold circuit", "PDIP-8", 3.60),
                            ("SHC615AU", "Texas Instruments", "Wideband high-speed track-and-hold amplifier", "SOIC-16", 26.40),
                        ],
                    },
                    {
                        "id": "transconductance-amplifiers", "name": "Transconductance amplifiers & laser drivers",
                        "aliases": ["ota", "transconductance amplifier", "laser driver"],
                        "parts": [
                            ("LM13700N", "Texas Instruments", "Dual operational transconductance amplifier with buffers", "PDIP-16", 1.85),
                            ("OPA860ID", "Texas Instruments", "Wide-bandwidth OTA and buffer (diamond transistor)", "SOIC-8", 3.20),
                            ("ONET1191PRGTR", "Texas Instruments", "11.3 Gbps laser diode driver", "VQFN-16", 15.70),
                        ],
                    },
                    {
                        "id": "transimpedance-amplifiers", "name": "Transimpedance amplifiers",
                        "aliases": ["tia", "transimpedance amplifier", "photodiode amplifier"],
                        "parts": [
                            ("OPA855IDSGT", "Texas Instruments", "8 GHz gain-bandwidth decompensated transimpedance amplifier", "SOT-563", 5.90),
                            ("OPA858IDSGT", "Texas Instruments", "5.5 GHz gain-bandwidth FET-input TIA", "SOT-563", 6.30),
                            ("OPA857IRGTT", "Texas Instruments", "Programmable-gain transimpedance amplifier, 125 MHz", "VQFN-16", 8.40),
                        ],
                    },
                    {
                        "id": "video-amplifiers", "name": "Video amplifiers",
                        "aliases": ["video amplifier", "video buffer", "sync separator"],
                        "parts": [
                            ("THS7314D", "Texas Instruments", "3-channel SDTV video amplifier with 6 dB gain", "SOIC-8", 1.60),
                            ("OPA361AIDCKR", "Texas Instruments", "Video amplifier with internal filter and sync", "SC-70-6", 1.95),
                            ("LM1881N", "Texas Instruments", "Video sync separator", "PDIP-8", 2.40),
                        ],
                    },
                ],
            },
        ],
    },
    {
        "id": "audio-haptics-piezo", "name": "Audio, haptics & piezo",
        "aliases": ["audio", "haptics", "piezo", "class d amplifier", "audio codec"],
        "parts": [
            ("TPA3116D2DADR", "Texas Instruments", "Class-D stereo audio amplifier, 2 x 50 W", "HTSSOP-32", 4.20),
            ("PAM8403", "Diodes Incorporated", "Class-D stereo audio amplifier, 2 x 3 W", "SOP-16", 0.38),
            ("DRV2605LDGSR", "Texas Instruments", "Haptic driver for LRA and ERM with effect library", "VSSOP-10", 2.10),
            ("PCM5102APWR", "Texas Instruments", "32-bit 384 kHz stereo audio DAC with PLL", "TSSOP-20", 3.40),
            ("MAX98357AETE+T", "Analog Devices Maxim", "I2S Class-D audio amplifier, 3.2 W", "TQFN-16", 2.85),
        ],
    },
    {
        "id": "battery-management", "name": "Battery management ICs",
        "aliases": ["battery management", "battery charger", "bms", "fuel gauge"],
        "parts": [
            ("TP4056", "NanJing Top Power", "1 A single-cell Li-ion linear charger with protection", "SOP-8", 0.28),
            ("BQ24074RGTR", "Texas Instruments", "1.5 A single-cell Li-ion charger with power path", "VQFN-16", 2.30),
            ("BQ27441DRZR-G1A", "Texas Instruments", "Single-cell Li-ion battery fuel gauge, Impedance Track", "WSON-12", 3.15),
            ("DW01A", "Fortune Semiconductor", "Single-cell Li-ion battery protection IC", "SOT-23-6", 0.09),
            ("BQ76952PFBR", "Texas Instruments", "3 to 16 cell battery monitor and protector", "TQFP-48", 7.90),
        ],
    },
    {
        "id": "clocks-timing", "name": "Clocks & timing",
        "aliases": ["clock", "timing", "oscillator", "crystal", "timer", "pll"],
        "parts": [
            ("NE555P", "Texas Instruments", "Precision timer, astable / monostable", "PDIP-8", 0.48),
            ("ABM8-16.000MHZ-B2-T", "Abracon", "16 MHz crystal, 18 pF, +/-20 ppm", "SMD 3225", 0.42),
            ("ECS-2520MV-250-BN-TR", "ECS Inc", "25 MHz MEMS oscillator, 3.3 V", "SMD 2520", 1.08),
            ("CDCE913PWR", "Texas Instruments", "Programmable 1-PLL clock generator with I2C", "TSSOP-14", 2.60),
            ("DS3231SN#", "Analog Devices Maxim", "Extremely accurate I2C RTC with integrated TCXO", "SOIC-16", 6.85),
        ],
    },
    {
        "id": "data-converters", "name": "Data converters",
        "aliases": ["adc", "dac", "data converter", "analog to digital", "digital to analog"],
        "parts": [
            ("ADS1115IDGSR", "Texas Instruments", "16-bit 860 SPS 4-channel delta-sigma ADC with PGA, I2C", "VSSOP-10", 3.80),
            ("ADS1256IDBR", "Texas Instruments", "24-bit 30 kSPS 8-channel delta-sigma ADC", "SSOP-28", 12.40),
            ("MCP4725A0T-E/CH", "Microchip", "12-bit DAC with EEPROM and I2C", "SOT-23-6", 1.10),
            ("ADS8688IDBTR", "Texas Instruments", "16-bit 500 kSPS 8-channel SAR ADC, bipolar input", "TSSOP-38", 18.60),
            ("PCF8591T/2", "NXP", "8-bit 4-channel ADC with single DAC output, I2C", "SOIC-16", 1.95),
        ],
    },
    {
        "id": "dlp-products", "name": "DLP products",
        "aliases": ["dlp", "dmd", "digital micromirror", "projector chipset"],
        "parts": [
            ("DLP3010FQF", "Texas Instruments", "0.3-inch 720p DLP digital micromirror device", "FQF-100", 68.00),
            ("DLPC3439FZEZ", "Texas Instruments", "DLP display controller for 0.3 to 0.47 inch DMDs", "NFBGA-201", 34.50),
            ("DLPA2005RTQR", "Texas Instruments", "DLP integrated PMIC and LED driver", "VQFN-56", 9.80),
        ],
    },
    {
        "id": "interface", "name": "Interface",
        "aliases": ["interface", "transceiver", "uart", "can", "rs485", "rs232", "usb bridge"],
        "parts": [
            ("MCP2515-I/SO", "Microchip", "Stand-alone CAN 2.0B controller with SPI", "SOIC-18", 2.05),
            ("SN65HVD230DR", "Texas Instruments", "3.3 V CAN transceiver, 1 Mbps", "SOIC-8", 1.62),
            ("SN65HVD72DR", "Texas Instruments", "3.3 V half-duplex RS-485 transceiver, 20 Mbps", "SOIC-8", 1.48),
            ("MAX3232CSE+", "Analog Devices Maxim", "3 V to 5.5 V dual RS-232 transceiver", "SOIC-16", 2.55),
            ("FT232RL-REEL", "FTDI", "USB to serial UART interface IC", "SSOP-28", 4.70),
            ("CH340G", "WCH", "USB to serial converter, low cost", "SOIC-16", 0.55),
        ],
    },
    {
        "id": "isolation", "name": "Isolation",
        "aliases": ["isolator", "isolation", "optocoupler", "digital isolator"],
        "parts": [
            ("ISO7741DWR", "Texas Instruments", "Quad-channel reinforced digital isolator, 100 Mbps", "SOIC-16 wide", 3.60),
            ("ADUM1201ARZ", "Analog Devices", "Dual-channel digital isolator, 1 Mbps", "SOIC-8", 2.90),
            ("6N137", "Onsemi", "10 Mbps high-speed optocoupler", "PDIP-8", 0.45),
            ("PC817X1NSZ0F", "Sharp", "General-purpose photocoupler, 5 kV isolation", "PDIP-4", 0.09),
        ],
    },
    {
        "id": "logic-voltage-translation", "name": "Logic & voltage translation",
        "aliases": ["logic", "level shifter", "voltage translator", "shift register", "logic gate"],
        "parts": [
            ("74HC595D", "Nexperia", "8-bit serial-in / parallel-out shift register", "SOIC-16", 0.22),
            ("TXS0108EPWR", "Texas Instruments", "8-bit bidirectional level translator, auto-direction", "TSSOP-20", 1.10),
            ("SN74LVC245APWR", "Texas Instruments", "Octal bus transceiver with 3-state outputs", "TSSOP-20", 0.42),
            ("SN74HC08N", "Texas Instruments", "Quad 2-input AND gate", "PDIP-14", 0.35),
            ("CD4017BE", "Texas Instruments", "Decade counter / divider with 10 decoded outputs", "PDIP-16", 0.52),
        ],
    },
    {
        "id": "mcu-processors", "name": "Microcontrollers (MCUs) & processors",
        "aliases": ["mcu", "microcontroller", "processor", "soc", "cpu", "embedded"],
        "parts": [
            ("STM32F103C8T6", "STMicroelectronics", "ARM Cortex-M3 MCU, 72 MHz, 64 KB Flash, 20 KB RAM", "LQFP-48", 2.45),
            ("STM32F407VGT6", "STMicroelectronics", "ARM Cortex-M4F MCU, 168 MHz, 1 MB Flash, FPU", "LQFP-100", 9.80),
            ("STM32H743VIT6", "STMicroelectronics", "ARM Cortex-M7 MCU, 480 MHz, 2 MB Flash", "LQFP-100", 16.20),
            ("ATMEGA328P-PU", "Microchip", "8-bit AVR MCU, 32 KB Flash, 20 MHz, DIP", "PDIP-28", 2.15),
            ("ATSAMD21G18A-AU", "Microchip", "ARM Cortex-M0+ MCU, 48 MHz, 256 KB Flash, USB", "TQFP-48", 5.40),
            ("RP2040", "Raspberry Pi", "Dual ARM Cortex-M0+ MCU, 133 MHz, 264 KB SRAM, PIO", "QFN-56", 0.95),
            ("MSP430G2553IN20", "Texas Instruments", "16-bit ultra-low-power MCU, 16 KB Flash", "PDIP-20", 3.05),
            ("PIC16F877A-I/P", "Microchip", "8-bit MCU, 14 KB Flash, 33 I/O", "PDIP-40", 4.60),
            ("AM3358BZCZA100", "Texas Instruments", "Sitara ARM Cortex-A8 processor, 1 GHz, 3D graphics", "NFBGA-324", 22.00),
        ],
    },
    {
        "id": "motor-drivers", "name": "Motor drivers",
        "aliases": ["motor driver", "stepper driver", "h-bridge", "bldc driver", "motor controller"],
        "parts": [
            ("DRV8825PWPR", "Texas Instruments", "Stepper motor driver with 1/32 microstepping, 2.5 A", "HTSSOP-28", 3.10),
            ("A4988SETTR-T", "Allegro MicroSystems", "DMOS microstepping driver with translator, 2 A", "QFN-28", 2.20),
            ("L298N", "STMicroelectronics", "Dual full-bridge motor driver, 2 A per channel", "Multiwatt-15", 1.90),
            ("TB6612FNG", "Toshiba", "Dual H-bridge DC motor driver, 1.2 A", "SSOP-24", 1.45),
            ("DRV8313PWPR", "Texas Instruments", "Triple half-bridge BLDC motor driver, 2.5 A", "HTSSOP-28", 3.55),
        ],
    },
    {
        "id": "passive-discrete", "name": "Passive & discrete",
        "aliases": ["passive", "discrete", "resistor", "capacitor", "inductor", "diode", "transistor", "mosfet", "pcb"],
        "parts": [
            ("RC0805FR-0710KL", "Yageo", "10 kOhm 1% thick-film chip resistor, 0805", "0805", 0.012),
            ("CL10A106MP8NNNC", "Samsung Electro-Mechanics", "10 uF 10 V X5R MLCC ceramic capacitor", "0603", 0.028),
            ("UWT1V101MCL1GS", "Nichicon", "100 uF 35 V aluminium electrolytic, SMD", "SMD Can", 0.21),
            ("744314650", "Wurth Elektronik", "6.5 uH shielded power inductor, 5 A", "WE-LHMI 4020", 0.64),
            ("SS34", "Vishay", "3 A 40 V Schottky rectifier diode", "SMA/DO-214AC", 0.11),
            ("1N4148WS-7-F", "Diodes Incorporated", "75 V 150 mA fast switching diode", "SOD-323", 0.03),
            ("IRLZ44NPBF", "Infineon", "N-channel logic-level MOSFET, 55 V, 47 A", "TO-220AB", 1.34),
            ("SI2302CDS-T1-GE3", "Vishay", "N-channel MOSFET, 20 V, 2.6 A", "SOT-23", 0.16),
            ("BC547B", "Onsemi", "NPN general-purpose bipolar transistor, 45 V", "TO-92", 0.06),
        ],
    },
    {
        "id": "power-management", "name": "Power management",
        "aliases": ["power management", "pmic", "regulator", "ldo", "buck", "boost", "dc-dc", "converter"],
        "parts": [
            ("AMS1117-3.3", "Advanced Monolithic", "1 A low-dropout linear regulator, 3.3 V fixed", "SOT-223", 0.18),
            ("LM7805CT", "Texas Instruments", "1.5 A fixed 5 V positive linear regulator", "TO-220", 0.55),
            ("LM2596S-ADJ", "Texas Instruments", "3 A step-down switching regulator, adjustable", "TO-263-5", 1.55),
            ("TPS62840DLCR", "Texas Instruments", "750 mA buck converter, 60 nA quiescent current", "SOT-583", 1.20),
            ("TPS54331DR", "Texas Instruments", "3 A 28 V step-down converter with eco-mode", "SOIC-8", 1.35),
            ("MP1584EN-LF-Z", "Monolithic Power", "3 A, 1.5 MHz synchronous step-down converter", "SOIC-8", 0.86),
            ("MT3608", "Aerosemi", "2 A 1.2 MHz step-up boost converter", "SOT-23-6", 0.24),
        ],
    },
    {
        "id": "rf-microwave", "name": "RF & microwave",
        "aliases": ["rf", "microwave", "transceiver", "antenna", "sub-ghz"],
        "parts": [
            ("CC1101RGPR", "Texas Instruments", "Low-power sub-1 GHz RF transceiver", "VQFN-20", 3.10),
            ("NRF24L01P-R", "Nordic Semiconductor", "2.4 GHz RF transceiver, 2 Mbps", "QFN-20", 2.20),
            ("SX1276IMLTRT", "Semtech", "LoRa long-range sub-GHz transceiver, 137 to 1020 MHz", "QFN-28", 5.40),
            ("2450AT18A100E", "Johanson Technology", "2.45 GHz SMD chip antenna", "0805", 0.68),
        ],
    },
    {
        "id": "sensors", "name": "Sensors",
        "aliases": ["sensor", "temperature sensor", "imu", "accelerometer", "pressure sensor"],
        "parts": [
            ("BME280", "Bosch Sensortec", "Humidity, pressure and temperature sensor, I2C/SPI", "LGA-8", 5.20),
            ("MPU-6050", "TDK InvenSense", "6-axis MEMS gyroscope + accelerometer", "QFN-24", 3.45),
            ("DS18B20+", "Analog Devices Maxim", "1-Wire digital thermometer, -55 to +125 C", "TO-92", 3.90),
            ("LM35DZ/NOPB", "Texas Instruments", "Precision centigrade temperature sensor, analog out", "TO-92", 1.60),
            ("TMP117AIDRVR", "Texas Instruments", "High-accuracy digital temperature sensor, +/-0.1 C", "WSON-6", 4.75),
            ("VL53L0CXV0DH/1", "STMicroelectronics", "Time-of-flight ranging sensor, 2 m", "Optical LGA-12", 5.95),
            ("HDC1080DMBR", "Texas Instruments", "Low-power humidity and temperature digital sensor", "WSON-6", 2.95),
        ],
    },
    {
        "id": "switches-multiplexers", "name": "Switches & multiplexers",
        "aliases": ["switch", "multiplexer", "mux", "analog switch", "load switch"],
        "parts": [
            ("CD4051BE", "Texas Instruments", "Single 8-channel analog multiplexer / demultiplexer", "PDIP-16", 0.48),
            ("CD74HC4067M96", "Texas Instruments", "16-channel analog multiplexer / demultiplexer", "SOIC-24", 1.10),
            ("TMUX1208PWR", "Texas Instruments", "8-channel low-leakage analog multiplexer, 1.08 V to 5.5 V", "TSSOP-16", 1.20),
            ("TPS22918DBVR", "Texas Instruments", "5.5 V 2 A load switch with controlled slew rate", "SOT-23-6", 0.58),
        ],
    },
    {
        "id": "wireless-connectivity", "name": "Wireless connectivity",
        "aliases": ["wireless", "wifi", "wi-fi", "bluetooth", "ble", "zigbee", "lora", "thread"],
        "parts": [
            ("ESP32-WROOM-32E", "Espressif", "Wi-Fi + Bluetooth LE SoC module, dual-core 240 MHz", "SMD Module", 3.10),
            ("ESP32-S3-WROOM-1-N16R8", "Espressif", "Wi-Fi + BLE module, 16 MB Flash, 8 MB PSRAM, AI accel", "SMD Module", 4.35),
            ("ESP8266EX", "Espressif", "Wi-Fi SoC, 32-bit Tensilica L106, 80 MHz", "QFN-32", 1.35),
            ("NRF52840-QIAA-R", "Nordic Semiconductor", "BLE 5.4 / Thread / Zigbee SoC, Cortex-M4F", "aQFN-73", 6.75),
            ("CC2652R1FRGZR", "Texas Instruments", "Multiprotocol 2.4 GHz wireless MCU, Zigbee / Thread / BLE", "VQFN-48", 6.20),
            ("RFM95W-868S2", "HopeRF", "LoRa long-range transceiver module, 868 MHz", "SMD Module", 6.80),
        ],
    },
    # Categories outside the semiconductor-vendor tree that this tool still needs,
    # because people search for them constantly when sourcing a board.
    {
        "id": "fpga-programmable-logic", "name": "FPGA & programmable logic",
        "aliases": ["fpga", "cpld", "vlsi", "programmable logic", "gate array"],
        "parts": [
            ("XC7A35T-1FTG256C", "AMD Xilinx", "Artix-7 FPGA, 33280 logic cells, 256-pin BGA", "FTBGA-256", 48.90),
            ("XC7A100T-2FGG484I", "AMD Xilinx", "Artix-7 FPGA, 101440 logic cells, industrial grade", "FBGA-484", 138.00),
            ("XC6SLX9-2TQG144C", "AMD Xilinx", "Spartan-6 FPGA, 9152 logic cells", "TQFP-144", 22.40),
            ("10M08SAU169C8G", "Intel Altera", "MAX 10 FPGA, 8000 LEs, on-chip flash + ADC", "UBGA-169", 19.75),
            ("EP4CE22F17C6N", "Intel Altera", "Cyclone IV E FPGA, 22320 logic elements", "FBGA-256", 32.10),
            ("ICE40UP5K-SG48ITR", "Lattice Semiconductor", "iCE40 UltraPlus FPGA, 5280 LUTs, open toolchain", "QFN-48", 6.85),
            ("LCMXO2-1200HC-4TG100C", "Lattice Semiconductor", "MachXO2 CPLD/FPGA, 1280 LUTs", "TQFP-100", 7.90),
            ("ATF16V8B-15PU", "Microchip", "EEPROM SPLD, 8 macrocells, 15 ns", "PDIP-20", 2.30),
        ],
    },
    {
        "id": "memory", "name": "Memory",
        "aliases": ["memory", "flash", "sram", "dram", "sdram", "eeprom", "nor", "nand"],
        "parts": [
            ("W25Q128JVSIQ", "Winbond", "128 Mbit serial NOR Flash, SPI / Quad-SPI", "SOIC-8", 1.25),
            ("MT41K256M16TW-107", "Micron", "DDR3L SDRAM, 4 Gbit, 933 MHz, 1.35 V", "FBGA-96", 6.40),
            ("IS42S16400J-7TLI", "ISSI", "64 Mbit SDRAM, 143 MHz, 4M x 16", "TSOP-54", 2.80),
            ("CY7C1041GN30-10ZSXI", "Infineon Cypress", "4 Mbit async SRAM, 10 ns, 256K x 16", "TSOP-44", 8.95),
            ("AT24C256C-SSHL-T", "Microchip", "256 Kbit I2C serial EEPROM", "SOIC-8", 0.42),
        ],
    },
    {
        "id": "connectors", "name": "Connectors & PCB hardware",
        "aliases": ["connector", "header", "socket", "receptacle", "terminal block", "usb connector"],
        "parts": [
            ("61300411121", "Wurth Elektronik", "2.54 mm pin header, 4-pin, vertical THT", "THT Header", 0.19),
            ("10118193-0001LF", "Amphenol", "Micro USB Type-B receptacle, SMD", "SMD", 0.62),
            ("USB4105-GF-A", "GCT", "USB Type-C receptacle, 16-pin, 2.0", "SMD", 0.88),
            ("1935161", "Phoenix Contact", "2-way PCB screw terminal block, 5 mm pitch", "THT", 1.05),
            ("B3F-1000", "Omron", "Tactile switch, 6x6 mm, 130 gf, THT", "THT", 0.29),
        ],
    },
    {
        "id": "optoelectronics", "name": "LEDs & optoelectronics",
        "aliases": ["led", "display", "optoelectronics", "oled", "seven segment"],
        "parts": [
            ("WP7113SGC", "Kingbright", "5 mm green LED, 568 nm, 20 mA", "THT 5mm", 0.14),
            ("WS2812B", "Worldsemi", "Intelligent RGB LED with integrated controller", "PLCC-4 5050", 0.11),
            ("SSD1306", "Solomon Systech", "128x64 OLED display driver with controller", "COG", 1.35),
            ("LTST-C170KGKT", "Lite-On", "Green chip LED, 0805, 20 mA", "0805", 0.09),
        ],
    },
]


# --------------------------------------------------------------------------- #
# Supplier catalogue merge
# --------------------------------------------------------------------------- #

# The hand-written tree above goes deep on semiconductors and carries reference
# part numbers. The generated catalogue goes wide: the full breadth of what the
# distributors actually stock -- passives, connectors, electromechanical, cable,
# enclosures, test gear -- pulled from a distributor's own published tree.
#
# The two are merged rather than replaced. Where they name the same category the
# hand-written node wins: it has tuned aliases and real part numbers behind it,
# and it is listed first so match() resolves ties in its favour.
try:
    from .catalogue import SUPPLIER_TREE
except ImportError:            # catalogue not generated yet -- curated only
    SUPPLIER_TREE = []


def _curated_index(roots):
    """Every curated name and alias, mapped to the node that owns it."""
    owners = {}
    for node, _trail in _walk(roots, []):
        for label in [node["name"]] + list(node.get("aliases") or []):
            owners.setdefault(_squash(label), node)
    owners.pop("", None)
    return owners


def _merge_supplier(nodes, owners):
    """Fold the supplier catalogue into the curated tree.

    A supplier category the curated tree already names is not duplicated --
    but anything genuinely new below it is grafted onto the curated node, so
    the extra depth is kept and stays where it belongs instead of being
    promoted to the top of the browser.
    """
    out = []
    for node in nodes:
        key = _squash(node["name"])
        owner = owners.get(key)
        children = _merge_supplier(node.get("children") or [], owners)
        if owner is not None:
            if children:
                owner.setdefault("children", []).extend(children)
            continue
        entry = dict(node)
        if children:
            entry["children"] = children
        else:
            entry.pop("children", None)
        owners[key] = entry
        out.append(entry)
    return out


def _build_tree():
    # Deep-copied because the merge grafts supplier branches onto curated
    # nodes; the literal above stays exactly as written.
    roots = copy.deepcopy(CURATED_TREE)
    return roots + _merge_supplier(SUPPLIER_TREE, _curated_index(roots))


# --------------------------------------------------------------------------- #
# Traversal helpers
# --------------------------------------------------------------------------- #

def _walk(nodes, path):
    for node in nodes:
        trail = path + [node["name"]]
        yield node, trail
        for child, child_trail in _walk(node.get("children") or [], trail):
            yield child, child_trail


def iter_nodes():
    """Every node in the tree, with its breadcrumb trail."""
    yield from _walk(TREE, [])


def by_id():
    return {node["id"]: (node, trail) for node, trail in iter_nodes()}


_INDEX = None


def index():
    global _INDEX
    if _INDEX is None:
        _INDEX = by_id()
    return _INDEX


def breadcrumb(node_id):
    entry = index().get(node_id)
    return list(entry[1]) if entry else []


def all_parts():
    """Flatten every reference part, tagged with its category and breadcrumb.

    A part listed under two branches is emitted once, under the first.
    """
    seen = set()
    for node, trail in iter_nodes():
        for mpn, manufacturer, description, package, price in node.get("parts") or []:
            key = mpn.upper()
            if key in seen:
                continue
            seen.add(key)
            yield {
                "mpn": mpn,
                "manufacturer": manufacturer,
                "description": description,
                "package": package,
                "price": price,
                "categoryId": node["id"],
                "category": node["name"],
                "breadcrumb": list(trail),
            }


def search_term(node):
    """The keyword to actually send a distributor for this category.

    Category names carry qualifiers that hurt a keyword search -- "Operational
    amplifiers (op amps)" or "Precision op amps (Vos < 1 mV)" -- so the
    parenthetical is stripped before the term goes upstream.
    """
    if not node:
        return None
    # Catalogue nodes carry a keyword worked out at generation time: a
    # distributor's own browse-tree names ("Linear - Amplifiers -
    # Instrumentation, OP Amps, Buffer Amps") find nothing typed literally.
    if node.get("term"):
        return node["term"]
    name = re.sub(r"\s*\([^)]*\)", "", node["name"]).strip()
    return name or node["name"]


def subtree_parts(node_id):
    """Every reference part at or below a node, nearest branch first.

    A category search has to reach the children: "op amps" is a parent whose own
    parts list is empty, and the useful results all live one level down.
    """
    entry = index().get(node_id)
    if not entry:
        return []

    collected, seen = [], set()

    def descend(node, depth):
        for mpn, manufacturer, description, package, price in node.get("parts") or []:
            key = mpn.upper()
            if key in seen:
                continue
            seen.add(key)
            collected.append({
                "mpn": mpn,
                "manufacturer": manufacturer,
                "description": description,
                "package": package,
                "price": price,
                "categoryId": node["id"],
                "category": node["name"],
                "depth": depth,
            })
        for child in node.get("children") or []:
            descend(child, depth + 1)

    descend(entry[0], 0)
    return collected


def public_tree():
    """The tree as the browser needs it: names, ids and part counts only."""
    def convert(nodes):
        out = []
        for node in nodes:
            children = convert(node.get("children") or [])
            direct = len(node.get("parts") or [])
            total = direct + sum(c["totalParts"] for c in children)
            out.append({
                "id": node["id"],
                "name": node["name"],
                "parts": total,
                "totalParts": total,
                # What the distributor lists under this category. Reference
                # parts and distributor line items are different things and the
                # UI labels them differently, so they stay separate fields.
                "supplierParts": node.get("supplierParts") or 0,
                "children": children,
            })
        return out
    return convert(TREE)


# --------------------------------------------------------------------------- #
# Query matching
# --------------------------------------------------------------------------- #

_MPN_HINT = re.compile(r"[a-z]+[-_]?\d{2,}", re.I)


def _normalise(text):
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _squash(text):
    return re.sub(r"\s+", " ", _normalise(text))


# Built once, here rather than beside the literal, because the merge needs the
# normalisers defined above to compare names.
TREE = _build_tree()


def looks_like_part_number(query):
    """True for things like STM32F103C8T6 or INA219AIDR.

    Category expansion must never hijack an exact part-number lookup, so this
    check gates it.
    """
    q = (query or "").strip()
    if " " in q:
        return False
    return bool(_MPN_HINT.search(q)) and any(ch.isdigit() for ch in q)


def match(query):
    """Resolve a free-text query to a category node.

    Returns (node, breadcrumb) or (None, None). Only confident matches count --
    the whole normalised query has to correspond to a category name or alias, so
    a stray word never redirects a specific search.
    """
    if not query or looks_like_part_number(query):
        return None, None

    q = _squash(query)
    if not q or len(q) < 3:
        return None, None

    best = None
    for node, trail in iter_nodes():
        candidates = [node["name"]] + list(node.get("aliases") or [])
        for candidate in candidates:
            c = _squash(candidate)
            if not c:
                continue
            if q == c:
                score = 100
            elif q.rstrip("s") == c.rstrip("s"):
                score = 95
            elif c.startswith(q + " ") or q.startswith(c + " "):
                score = 70
            elif len(q) >= 5 and q in c:
                score = 55
            else:
                continue
            # Prefer the most specific match, then the longest alias.
            depth = len(trail)
            ranked = (score, depth, len(c))
            if best is None or ranked > best[0]:
                best = (ranked, node, trail)

    if not best or best[0][0] < 55:
        return None, None
    return best[1], list(best[2])
