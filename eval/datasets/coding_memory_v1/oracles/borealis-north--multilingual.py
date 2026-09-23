"""Immutable oracle for borealis-north:multilingual."""
import service


def main():
    assert service.localized_status() == 'borealis-north fr-CA statut-actif', service.localized_status()


if __name__ == "__main__":
    main()
