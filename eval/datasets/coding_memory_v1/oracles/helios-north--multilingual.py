"""Immutable oracle for helios-north:multilingual."""
import service


def main():
    assert service.localized_status() == 'helios-north fr-CA statut-actif', service.localized_status()


if __name__ == "__main__":
    main()
