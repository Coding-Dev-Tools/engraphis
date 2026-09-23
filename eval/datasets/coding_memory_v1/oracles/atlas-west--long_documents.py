"""Immutable oracle for atlas-west:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The vault path retains 15 days and uses America/New_York.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
