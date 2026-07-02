# Configuration réseau de l'hôte Proxmox (proxmox-test)

Explication de `/etc/network/interfaces` telle que trouvée sur cet hôte (configuration
préexistante, non mise en place par nous), pour comprendre avant de la modifier.

## Interfaces physiques (NIC)

- `nic0` — sans commentaire. Utilisée comme port du bridge `vmbr0` (management, natif,
  pas de VLAN). En pratique câblée sur un port switch en **accès simple**, pas un trunk.
- `nic1` — commentaire `#VLAN 1 (192.0.0.X)`. Port du bridge `plastigray`. Réseau dédié,
  sans rapport avec notre problème.
- `nic2` — commentaire `#TRUNK`. Port du bridge `tous_vlans`. C'est la carte branchée sur
  un port switch configuré en **trunk 802.1Q**, censée transporter plusieurs VLANs
  (5, 10, 20, 30, 40, 100, 150, 200, 250).

## Bridges "classiques" (un seul VLAN, ou natif)

- **`vmbr0`** — bridge de management de l'hôte. IP `10.1.5.61/24`, passerelle
  `10.1.5.254`. Port : `nic0`. Pas de tag VLAN (`bridge-vlan-aware` absent) : tout ce qui
  y transite sort en natif (non tagué) sur `nic0`. C'est aussi le bridge par défaut
  proposé pour les VM si on ne précise rien.
- **`interne`** — bridge sans aucun port physique (`bridge-ports none`). Réseau
  strictement privé, invisible en dehors de l'hôte : seules les VM qui y sont connectées
  peuvent se parler entre elles. Le commentaire `#vlan5 interne` suggère qu'il devait à
  l'origine correspondre au VLAN 5, mais il n'y est pas relié (volontairement, pour isoler
  ce réseau qui réutilise les mêmes IP que la production).
- **`plastigray`** — bridge sur `nic1`, réseau `VLAN 1 (192.0.0.X)`. Sans rapport avec
  notre sujet.

## Bridges "VLAN-aware" (trunk, plusieurs VLANs sur un seul bridge)

- **`tous_vlans`** — bridge VLAN-aware (`bridge-vlan-aware yes`), port `nic2` (le
  trunk). Déclare `bridge-vids 5 10 20 30 40 100 150 200 250` : ces VLANs sont
  autorisés à transiter sur ce bridge/ce trunk. C'est le bridge à utiliser pour toute VM
  qui doit être taguée dans un de ces VLANs et sortir par le vrai trunk (`nic2`).
- **`FAKE_vlans`** — bridge VLAN-aware, mais `bridge-ports none` : aucun port physique.
  Sert de brique interne pour créer des sous-interfaces VLAN "virtuelles" sans sortir sur
  le réseau physique (voir `vlan30` ci-dessous).
- **`vlan30`** — pas un bridge mais une interface VLAN classique (`vlan-raw-device
  FAKE_vlans`), donc une sous-interface taguée VLAN 30 posée sur le bridge fictif
  `FAKE_vlans`. Comme `FAKE_vlans` n'a pas de port physique, cette interface ne sort
  jamais sur le réseau réel : c'est un VLAN 30 "local à l'hôte", utilisé en interne.
- **`CR_vlan30`** — bridge classique dont le port est `vlan30` (l'interface décrite
  juste au-dessus). Sert probablement à raccorder un conteneur/une VM à ce VLAN 30
  "local", isolé du réseau physique.

## Ce qui explique notre problème

La VM 100 (passerelle) avait initialement `net0` sur `bridge=vmbr0` avec `tag=30`.
Comme `vmbr0` n'est pas VLAN-aware, Proxmox simule le tag en créant à la volée une
sous-interface `nic0.30` (visible dans `brctl show` sous `vmbr0v30`). Le trafic taggé
VLAN 30 sortait donc par `nic0`, qui n'est **pas** le port trunk (c'est `nic2` qui l'est,
d'après son commentaire `#TRUNK` et le `bridge-vids` de `tous_vlans` qui inclut déjà 30).
Résultat : les trames taguées VLAN 30 partaient sur un lien qui n'est probablement pas
configuré côté switch pour transporter ce VLAN → pas de réponse au ping.

Nous avons déjà corrigé la VM 100 pour utiliser `bridge=tous_vlans` avec `tag=30` (le
bon trunk). Mais le ping échoue toujours car **l'hôte Proxmox lui-même n'a aucune IP dans
le VLAN 30** — ni sur `vmbr0` (natif, VLAN 5 seulement), ni ailleurs. Ce n'est donc pas
(encore) un problème de VM, mais l'absence d'une interface de test côté hôte.

