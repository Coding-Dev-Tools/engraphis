"""Immutable oracle for grove-north:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The delivery path retains 38 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
