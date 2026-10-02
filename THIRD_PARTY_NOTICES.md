# Third-party model notices

EviDrug uses third-party software and pretrained model artifacts. The project's MIT License
applies only to original EviDrug code and documentation. It does not replace the licenses or
terms listed below.

This file covers the model runtimes distributed by this repository. Transitive Python, JavaScript
and container dependencies retain their own notices in their source distributions, package
metadata or container filesystem.

## ADMET-AI 1.4.0

- Component: ADMET prediction software and model weights bundled in the `admet-ai==1.4.0`
  Python distribution
- Upstream: https://github.com/swansonk14/admet_ai/tree/v_1.4.0
- License: MIT
- Copyright: Copyright (c) 2025 Kyle Swanson
- Local use: `experiments/admet-smoke`; installed into the ADMET and worker container images
- Changes: EviDrug supplies an isolated runtime wrapper and does not modify the upstream weights
- License copy: `experiments/admet-smoke/LICENSES/ADMET-AI-LICENSE.txt`

ADMET-AI reports that its models were trained on datasets obtained through the Therapeutics Data
Commons. The source datasets may have dataset-specific terms. EviDrug redistributes the packaged
inference artifacts, not those training datasets.

## DeepPurpose 0.1.5

- Component: DeepPurpose drug-target interaction software
- Upstream: https://github.com/kexinhuang12345/DeepPurpose
- License: BSD-3-Clause
- Copyright: Copyright (c) 2020, Kexin Huang, Tianfan Fu
- Local use: `experiments/deeppurpose-dta-smoke`; installed into the DTA and worker container images
- Changes: EviDrug supplies an inference-only runtime, a network-disabled download shim and a
  minimal compatibility namespace
- License copy: `experiments/deeppurpose-dta-smoke/LICENSES/DeepPurpose-LICENSE.txt`

### DeepPurpose pretrained checkpoints

- Source dataset: DeepPurpose pretrained models, Harvard Dataverse,
  https://doi.org/10.7910/DVN/CNQV69
- Dataset license reported by Harvard Dataverse on 2026-10-02: CC0-1.0
- Files used:
  - datafile `4159715`, `model_cnn_cnn_bindingdb.zip`, pinned archive SHA-256
    `1f5c62863303d5057566b3b29be24e31cc138b361dfb8b085b53c8169d8c6829`
  - datafile `4204178`, `model_mpnn_cnn_bindingdb.zip`, pinned archive SHA-256
    `655119c3896a773a8ccf0e711ad263a3bfbcd0bab7ea5085cccb12e13908bc0c`
- Changes: archives are safely extracted and their checkpoint files are renamed for isolated
  runtime selection; model parameters are not modified

The checkpoint archive does not provide a machine-readable model card or split metadata. The
license permits redistribution, but scientific provenance and evaluation limitations remain
documented in `experiments/deeppurpose-dta-smoke/README.md`.

## CToxPred2

- Component: CToxPred2 RF-SSL cardiotoxicity models and preprocessing artifacts
- Upstream: https://github.com/issararab/CToxPred2
- Pinned upstream commit: `2a31aa119e27b6b69a5588d18a01f2a27fef4524`
- License: MIT
- Copyright: Copyright (c) 2024 Issar Arab
- Local use: `experiments/ctoxpred2-smoke`; copied into the CToxPred2 and worker container images
- Changes: EviDrug extracts only the pinned preprocessing pipeline and three random-forest model
  files and invokes them through an isolated JSON-lines runtime
- License copy: the image build downloads the pinned upstream license to
  `/opt/ctoxpred2/licenses/CToxPred2-LICENSE`

## PyBioMed

- Component: molecular descriptor implementation used by the CToxPred2 runtime
- Upstream: https://github.com/gadsbyfly/PyBioMed
- Pinned upstream commit: `45440d8a70b2aa2818762ceadb499dd3a1df90bc`
- Local use: copied into the CToxPred2 container image
- License copy: the image build copies upstream `LICENSE.txt` to
  `/opt/ctoxpred2/licenses/PyBioMed-LICENSE.txt`

## Hosted language models and public databases

EviDrug may call language models through a configured API. No hosted language-model weights are
included in this repository or its container images. API access remains subject to the provider's
service terms rather than the EviDrug repository license.

Open Targets, UniProt, Pharos, PubChem and other external data providers retain their respective
data licenses and terms. An API response being publicly accessible does not by itself grant EviDrug
the right to redistribute the provider's complete database.
