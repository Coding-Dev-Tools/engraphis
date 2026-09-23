"""Immutable oracle for atlas-north:multilingual."""
import service


def main():
    assert service.localized_status() == 'atlas-north fr-CA statut-actif', service.localized_status()


if __name__ == "__main__":
    main()
