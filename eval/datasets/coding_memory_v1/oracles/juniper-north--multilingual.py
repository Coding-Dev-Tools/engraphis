"""Immutable oracle for juniper-north:multilingual."""
import service


def main():
    assert service.localized_status() == 'juniper-north fr-CA statut-actif', service.localized_status()


if __name__ == "__main__":
    main()
