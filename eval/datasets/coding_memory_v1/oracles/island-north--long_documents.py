"""Immutable oracle for island-north:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The warehouse path retains 46 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
