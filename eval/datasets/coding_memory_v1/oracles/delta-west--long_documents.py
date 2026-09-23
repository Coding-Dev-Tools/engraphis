"""Immutable oracle for delta-west:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The index path retains 27 days and uses America/New_York.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
