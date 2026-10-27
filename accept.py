#!/usr/bin/env python3
"""Accepte les invitations GitHub en attente et les journalise.

Chaque exécution accepte les invitations de dépôt et d'organisation du
compte authentifié, puis ajoute une ligne à ``invitations_acceptees.csv``.
Relancer le programme accepte aussi les invitations arrivées entre-temps.

Le profil GitHub ne sépare pas le prénom du nom, et l'e-mail n'est lisible
que s'il est public. Le journal laisse le champ vide et l'explique dans
``remarque`` quand GitHub ne le publie pas.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


REPERTOIRE = Path(__file__).resolve().parent
JOURNAL_DEFAUT = REPERTOIRE / "invitations_acceptees.csv"

# Colonnes stables : les quatre demandées, plus de quoi identifier l'invitation.
COLONNES_JOURNAL = (
    "horodatage_utc",
    "id_invitation",
    "type",
    "cible",
    "permission",
    "username",
    "prenom",
    "nom",
    "email",
    "remarque",
)


class TypeInvitation(Enum):
    """Origine de l'invitation. Les deux points d'API ne se ressemblent pas."""

    DEPOT = "depot"
    ORGANISATION = "organisation"


class ErreurGitHub(Exception):
    """Appel GitHub impossible : jeton, réseau ou réponse d'erreur."""

    def __init__(self, message: str, statut: int | None = None) -> None:
        super().__init__(message)
        self.statut = statut


def jetons_disponibles() -> list[str]:
    """Jetons du compte, sans jamais les afficher.

    ``GITHUB_TOKEN`` et ``GH_TOKEN`` passent en premier. Un jeton fin peut
    lister les invitations sans droit de les accepter : le jeton enregistré
    par ``gh`` (souvent le scope ``repo``) est donc conservé en secours.
    """

    trouves: list[str] = []
    for nom in ("GITHUB_TOKEN", "GH_TOKEN"):
        valeur = os.environ.get(nom, "").strip()
        if valeur and valeur not in trouves:
            trouves.append(valeur)
    environnement = os.environ.copy()
    environnement.pop("GITHUB_TOKEN", None)
    environnement.pop("GH_TOKEN", None)
    try:
        resultat = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
            env=environnement,
        )
    except (OSError, subprocess.CalledProcessError):
        resultat = None
    if resultat is not None:
        jeton = resultat.stdout.strip()
        if jeton and jeton not in trouves:
            trouves.append(jeton)
    if not trouves:
        raise ErreurGitHub(
            "Aucun jeton. Définissez GITHUB_TOKEN ou GH_TOKEN, "
            "ou connectez-vous avec `gh auth login`. "
            "Accepter une invitation demande le scope repo."
        )
    return trouves


def separer_prenom_nom(nom_complet: str) -> tuple[str, str]:
    """Découpe le champ unique ``name`` de GitHub.

    GitHub ne stocke qu'une chaîne libre. Convention retenue :
    « Nom, Prénom » met le nom avant la virgule ; sinon le premier mot
    est le prénom et le reste est le nom. Un seul mot reste le prénom.
    """

    texte = " ".join(nom_complet.split())
    if not texte:
        return "", ""
    if "," in texte:
        gauche, _, droite = texte.partition(",")
        return " ".join(droite.split()), " ".join(gauche.split())
    mots = texte.split(" ")
    if len(mots) == 1:
        return mots[0], ""
    return mots[0], " ".join(mots[1:])


@dataclass(frozen=True)
class ProfilExpediteur:
    """Identité publique de la personne qui a envoyé l'invitation."""

    username: str
    prenom: str
    nom: str
    email: str
    nom_complet: str

    @classmethod
    def depuis_profil_public(cls, brut: dict) -> ProfilExpediteur:
        """Construit le profil depuis ``GET /users/{username}``."""

        nom_complet = (brut.get("name") or "").strip()
        prenom, nom = separer_prenom_nom(nom_complet)
        return cls(
            username=(brut.get("login") or "").strip(),
            prenom=prenom,
            nom=nom,
            email=(brut.get("email") or "").strip(),
            nom_complet=nom_complet,
        )

    @classmethod
    def sans_profil(cls, username: str) -> ProfilExpediteur:
        """Username connu, profil public illisible ou absent."""

        return cls(
            username=username.strip(),
            prenom="",
            nom="",
            email="",
            nom_complet="",
        )

    def remarque(self, expediteur_inconnu: bool = False) -> str:
        """Explique les champs laissés vides. Chaîne vide si tout est là."""

        if expediteur_inconnu:
            return (
                "GitHub ne fournit pas l'expéditeur "
                "d'une invitation d'organisation"
            )
        manques: list[str] = []
        if not self.username:
            manques.append("username absent")
        if not self.prenom and not self.nom:
            manques.append("nom absent du profil public")
        elif not self.nom:
            manques.append("nom de famille absent du profil public")
        if not self.email:
            manques.append("email non publié")
        return "; ".join(manques)


@dataclass(frozen=True)
class Invitation:
    """Invitation encore en attente pour le compte authentifié."""

    identifiant: str
    type: TypeInvitation
    cible: str
    permission: str
    username_expediteur: str
    expiree: bool

    @classmethod
    def depuis_depot(cls, brut: dict) -> Invitation:
        """Mappe un objet de ``GET /user/repository_invitations``."""

        depot = brut.get("repository") or {}
        expediteur = brut.get("inviter") or {}
        return cls(
            identifiant=str(brut.get("id") or ""),
            type=TypeInvitation.DEPOT,
            cible=(depot.get("full_name") or "").strip(),
            permission=(brut.get("permissions") or "").strip(),
            username_expediteur=(expediteur.get("login") or "").strip(),
            expiree=bool(brut.get("expired")),
        )

    @classmethod
    def depuis_organisation(cls, brut: dict) -> Invitation:
        """Mappe un objet de ``GET /user/memberships/orgs?state=pending``.

        Cette réponse décrit l'organisation, pas la personne qui a invité.
        """

        organisation = brut.get("organization") or {}
        login = (organisation.get("login") or "").strip()
        return cls(
            identifiant=f"org:{login}" if login else "org:",
            type=TypeInvitation.ORGANISATION,
            cible=login,
            permission=(brut.get("role") or "").strip(),
            username_expediteur="",
            expiree=False,
        )


class ClientGitHub:
    """Lectures et acceptations via l'API REST du compte authentifié."""

    RACINE = "https://api.github.com"
    VERSION_API = "2026-03-10"

    def __init__(self, jetons: str | list[str]) -> None:
        if isinstance(jetons, str):
            jetons = [jetons]
        propres: list[str] = []
        for jeton in jetons:
            jeton = jeton.strip()
            if jeton and jeton not in propres:
                propres.append(jeton)
        if not propres:
            raise ErreurGitHub("Le jeton GitHub est vide.")
        self._jetons = propres
        self._index = 0
        self._profils: dict[str, ProfilExpediteur] = {}
        self._secours_annonce = False

    def lister_invitations(self) -> list[Invitation]:
        """Invitations de dépôt, puis invitations d'organisation."""

        depots = [
            Invitation.depuis_depot(objet)
            for objet in self._pages("/user/repository_invitations")
        ]
        organisations = [
            Invitation.depuis_organisation(objet)
            for objet in self._pages("/user/memberships/orgs?state=pending")
        ]
        return depots + organisations

    def profil_expediteur(self, username: str) -> ProfilExpediteur:
        """Profil public, mis en cache. Un échec ne bloque pas l'acceptation."""

        cle = username.strip()
        if not cle:
            return ProfilExpediteur.sans_profil("")
        if cle in self._profils:
            return self._profils[cle]
        try:
            charge = self._json("GET", f"/users/{cle}")
        except ErreurGitHub:
            profil = ProfilExpediteur.sans_profil(cle)
        else:
            profil = (
                ProfilExpediteur.depuis_profil_public(charge)
                if isinstance(charge, dict)
                else ProfilExpediteur.sans_profil(cle)
            )
        self._profils[cle] = profil
        return profil

    def accepter(self, invitation: Invitation) -> None:
        """Accepte une invitation. 204 pour un dépôt, 200 pour une organisation."""

        if invitation.type is TypeInvitation.DEPOT:
            if not invitation.identifiant:
                raise ErreurGitHub("Invitation de dépôt sans identifiant.")
            self._json(
                "PATCH",
                f"/user/repository_invitations/{invitation.identifiant}",
            )
            return
        if not invitation.cible:
            raise ErreurGitHub("Invitation d'organisation sans nom.")
        self._json(
            "PATCH",
            f"/user/memberships/orgs/{invitation.cible}",
            {"state": "active"},
        )

    def _pages(self, chemin: str) -> list[dict]:
        """Suit ``Link: rel=next`` jusqu'à la dernière page."""

        resultats: list[dict] = []
        separateur = "&" if "?" in chemin else "?"
        url: str | None = f"{chemin}{separateur}per_page=100"
        while url:
            charge, lien_suivant = self._json_et_suivant("GET", url)
            if not isinstance(charge, list):
                raise ErreurGitHub(f"Liste attendue pour {chemin}.")
            resultats.extend(objet for objet in charge if isinstance(objet, dict))
            url = lien_suivant
        return resultats

    def _json(
        self,
        methode: str,
        chemin: str,
        corps: dict | None = None,
    ) -> dict | list | None:
        charge, _lien = self._json_et_suivant(methode, chemin, corps)
        return charge

    def _json_et_suivant(
        self,
        methode: str,
        chemin: str,
        corps: dict | None = None,
    ) -> tuple[dict | list | None, str | None]:
        """Appelle GitHub. Sur un 403, réessaie avec le jeton suivant."""

        while True:
            try:
                return self._appeler(methode, chemin, corps)
            except ErreurGitHub as exc:
                secours_possible = (
                    exc.statut == 403 and self._index + 1 < len(self._jetons)
                )
                if not secours_possible:
                    if exc.statut == 403:
                        raise ErreurGitHub(
                            f"{exc} Le jeton doit pouvoir accepter les invitations "
                            "(scope repo).",
                            statut=403,
                        ) from exc
                    raise
                self._index += 1
                if not self._secours_annonce:
                    print(
                        "Ce jeton peut lire les invitations mais pas les accepter. "
                        "Nouvel essai avec la session GitHub CLI.",
                        file=sys.stderr,
                    )
                    self._secours_annonce = True

    def _appeler(
        self,
        methode: str,
        chemin: str,
        corps: dict | None,
    ) -> tuple[dict | list | None, str | None]:
        url = chemin if chemin.startswith("https://") else f"{self.RACINE}{chemin}"
        donnees = None if corps is None else json.dumps(corps).encode("utf-8")
        # PATCH sans corps : GitHub veut quand même une longueur explicite.
        if methode == "PATCH" and donnees is None:
            donnees = b""
        requete = urllib.request.Request(url, data=donnees, method=methode)
        requete.add_header("Accept", "application/vnd.github+json")
        requete.add_header("Authorization", f"Bearer {self._jetons[self._index]}")
        requete.add_header("X-GitHub-Api-Version", self.VERSION_API)
        requete.add_header("User-Agent", "accept-invitations")
        if corps is not None:
            requete.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(requete, timeout=30) as reponse:
                brut = reponse.read()
                suivant = lien_suivant(reponse.headers.get("Link"))
        except urllib.error.HTTPError as exc:
            message = message_erreur(exc.read())
            raise ErreurGitHub(
                f"GitHub a répondu {exc.code} pour {methode} {chemin} : {message}",
                statut=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise ErreurGitHub(f"Réseau indisponible : {exc.reason}") from exc
        if not brut:
            return None, suivant
        try:
            return json.loads(brut.decode("utf-8")), suivant
        except json.JSONDecodeError as exc:
            raise ErreurGitHub("Réponse GitHub illisible.") from exc


def lien_suivant(entete: str | None) -> str | None:
    """Extrait l'URL ``rel="next"`` de l'en-tête ``Link``."""

    if not entete:
        return None
    for morceau in entete.split(","):
        url, _, relation = morceau.partition(";")
        if 'rel="next"' in relation:
            return url.strip().lstrip("<").rstrip(">")
    return None


def message_erreur(brut: bytes) -> str:
    """Message GitHub, sans jamais y joindre le jeton."""

    if not brut:
        return "réponse vide"
    try:
        charge = json.loads(brut.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "réponse illisible"
    if isinstance(charge, dict) and charge.get("message"):
        return str(charge["message"])
    return "erreur sans message"


class JournalInvitations:
    """CSV ajouté en fin de fichier. Seules les acceptations réussies y entrent."""

    def __init__(self, chemin: Path) -> None:
        self.chemin = chemin

    def enregistrer(self, ligne: dict[str, str]) -> None:
        """Ajoute une ligne. Écrit l'en-tête si le fichier est nouveau ou vide."""

        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        nouveau = not self.chemin.exists() or self.chemin.stat().st_size == 0
        with self.chemin.open("a", encoding="utf-8", newline="") as fichier:
            ecrivain = csv.DictWriter(fichier, fieldnames=COLONNES_JOURNAL)
            if nouveau:
                ecrivain.writeheader()
            ecrivain.writerow({colonne: ligne.get(colonne, "") for colonne in COLONNES_JOURNAL})
            fichier.flush()


class AccepteurInvitations:
    """Lit les invitations, les accepte, puis enregistre l'expéditeur."""

    def __init__(
        self,
        client: ClientGitHub,
        journal: JournalInvitations,
        simuler: bool = False,
    ) -> None:
        self._client = client
        self._journal = journal
        self._simuler = simuler

    def executer(self) -> int:
        """Retourne 0 si tout est accepté, 1 si au moins une acceptation échoue."""

        invitations = self._client.lister_invitations()
        if not invitations:
            print("Aucune invitation en attente.")
            return 0

        actives = [invitation for invitation in invitations if not invitation.expiree]
        expirees = len(invitations) - len(actives)
        if self._simuler:
            print("Simulation : aucune invitation n'est acceptée.")
        print(f"{len(actives)} invitation(s) à traiter.")
        if expirees:
            print(f"{expirees} invitation(s) expirée(s) ignorée(s).")

        echecs = 0
        for invitation in actives:
            if not self._traiter(invitation):
                echecs += 1
        if not self._simuler:
            print(f"Journal : {self._journal.chemin}")
        return 1 if echecs else 0

    def _traiter(self, invitation: Invitation) -> bool:
        """Accepte puis journalise. Retourne False seulement si l'acceptation échoue."""

        expediteur_inconnu = (
            invitation.type is TypeInvitation.ORGANISATION
            and not invitation.username_expediteur
        )
        profil = self._client.profil_expediteur(invitation.username_expediteur)
        if self._simuler:
            self._afficher(invitation, profil, "simulée")
            return True
        try:
            self._client.accepter(invitation)
        except ErreurGitHub as exc:
            self._afficher(invitation, profil, f"échec ({exc})")
            return False
        ligne = self._ligne(invitation, profil, expediteur_inconnu)
        try:
            self._journal.enregistrer(ligne)
        except OSError as exc:
            print(
                f"Acceptée, mais le journal n'a pas pu être écrit : {exc}",
                file=sys.stderr,
            )
            print(",".join(ligne[colonne] for colonne in COLONNES_JOURNAL), file=sys.stderr)
            return False
        self._afficher(invitation, profil, "acceptée")
        return True

    def _ligne(
        self,
        invitation: Invitation,
        profil: ProfilExpediteur,
        expediteur_inconnu: bool,
    ) -> dict[str, str]:
        return {
            "horodatage_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "id_invitation": invitation.identifiant,
            "type": invitation.type.value,
            "cible": invitation.cible,
            "permission": invitation.permission,
            "username": profil.username,
            "prenom": profil.prenom,
            "nom": profil.nom,
            "email": profil.email,
            "remarque": profil.remarque(expediteur_inconnu),
        }

    def _afficher(
        self,
        invitation: Invitation,
        profil: ProfilExpediteur,
        etat: str,
    ) -> None:
        print(
            f"- {etat} : {invitation.type.value} {invitation.cible} "
            f"({invitation.permission}) | "
            f"username={profil.username or '—'} "
            f"prenom={profil.prenom or '—'} "
            f"nom={profil.nom or '—'} "
            f"email={profil.email or '—'}"
        )


def analyser_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    analyseur = argparse.ArgumentParser(
        description=(
            "Accepte les invitations GitHub en attente et enregistre "
            "username, prénom, nom et e-mail public de l'expéditeur."
        )
    )
    analyseur.add_argument(
        "--simuler",
        action="store_true",
        help="Affiche les invitations sans les accepter ni écrire le journal.",
    )
    analyseur.add_argument(
        "--journal",
        type=Path,
        default=JOURNAL_DEFAUT,
        help=f"CSV des invitations acceptées (défaut : {JOURNAL_DEFAUT.name}).",
    )
    return analyseur.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = analyser_arguments(argv)
    try:
        accepteur = AccepteurInvitations(
            ClientGitHub(jetons_disponibles()),
            JournalInvitations(options.journal),
            simuler=options.simuler,
        )
        return accepteur.executer()
    except ErreurGitHub as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
