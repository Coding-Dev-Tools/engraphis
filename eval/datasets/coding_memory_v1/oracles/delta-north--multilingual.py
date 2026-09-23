"""Immutable oracle for delta-north:multilingual."""
import service


def main():
    assert service.localized_status() == 'delta-north fr-CA statut-actif', service.localized_status()


if __name__ == "__main__":
    main()
