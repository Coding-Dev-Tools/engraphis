"""Immutable oracle for island-west:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The warehouse path retains 47 days and uses America/New_York.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
