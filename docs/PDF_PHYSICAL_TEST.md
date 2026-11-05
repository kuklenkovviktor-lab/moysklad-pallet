# GRIT PDF physical barcode test (isolated)

**Never run on the employer warehouse or on a production database.**
This branch is a draft test only. The usual GRIT main service remains unchanged.

## Prerequisites

- A separate Windows PC with Python 3.10+ and Git.
- Test phone and Bluetooth/HID scanner on the same **trusted private Wi-Fi**.
- Synthetic 6-page PDF: 3 product sheets alternating with 3 WB sticker pages.
- Printable physical item/QR labels with matching codes.

## Start a clean local test instance

1. Clone **this feature branch** into a separate directory:
   `git clone --branch test/pdf-physical-scan-20261105 https://github.com/kuklenkovviktor-lab/moysklad-pallet.git grit-physical-test`
2. In PowerShell: `cd grit-physical-test`
3. Run `powershell -ExecutionPolicy Bypass -File .\run_pdf_physical_test.ps1`. This installs dependencies into a local virtual environment and prints a one-time manager password.
4. PC: open `http://127.0.0.1:5001/manager-login` and sign in.
5. Phone on the same private LAN: open `http://<PC_LAN_IP>:5001/fbs`. Use a **test badge** generated in this isolated DB.
6. Manager: `http://127.0.0.1:5001/manager/fbs-pdf`. Upload the synthetic PDF, check all codes, then click **«Загрузить в тестовую сборку»**.
7. Test: item A → WB A is accepted; item B → WB A is rejected; WB A again is rejected as a duplicate; unknown WB is rejected.
8. Record scan strings and results. If scanner sends a payload other than the PDF's printed 11 digits, the current matcher will reject it.

The test is intentionally limited to 1–10 WB labels per import. No MoySklad or WB API tokens are accepted. Only a separate SQLite DB file named `grit_pdf_physical_test.db` enables the import. No stock, receiving or FBO operations are required. **Only synthetic data.**

## Safety

- The HTTP listener is intended for a trusted private LAN and temporary test-only credentials. Do not expose TCP 5001 to the public internet.
- Stop it with Ctrl+C. Remove the separate local directory after testing if it is no longer needed.
- This is not a production deployment or evidence of industrial reliability.
